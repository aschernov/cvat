
# Copyright (C) 2018 Intel Corporation
#
# SPDX-License-Identifier: MIT

from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest, QueryDict
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render
from rules.contrib.views import permission_required, objectgetter
from cvat.apps.authentication.decorators import login_required
from cvat.apps.engine.models import Task as TaskModel
from cvat.apps.engine import annotation, task

import django_rq
import subprocess
import fnmatch
import logging
import json
import os
import rq

from cvat.apps.engine.log import slogger

from .model_loader import ModelLoader, read_model_config, get_blob_props, get_model_label_map
from .image_loader import ImageLoader
import os.path as osp
from os import walk
import json
import fnmatch

def get_image_data(path_to_data):
    def get_image_key(item):
        return int(osp.splitext(osp.basename(item))[0])

    image_list = []
    for root, _, filenames in walk(path_to_data):
        for filename in fnmatch.filter(filenames, '*.jpg'):
                image_list.append(osp.join(root, filename))

    image_list.sort(key=get_image_key)
    return ImageLoader(image_list)

def load_model(model_file, weights_file, config_file):
    config = read_model_config(config_file)
    model = (model_file, weights_file)

    blob_params = get_blob_props(config)
    class_names = get_model_label_map(config)
    model =  ModelLoader(path_to_model=model, blob_params=blob_params)
    model.load()

    return model, class_names

def process_detections(detections, path_to_conv_script):
    import importlib.util
    spec = importlib.util.spec_from_file_location('converter', path_to_conv_script)
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    return converter.process_detections(detections)

def create_anno_container():
    return {
        "boxes": [],
        "polygons": [],
        "polylines": [],
        "points": [],
        "box_paths": [],
        "polygon_paths": [],
        "polyline_paths": [],
        "points_paths": [],
    }

def run_inference_engine_annotation(path_to_data, model_file, weights_file, config_file, convertation_file, job, update_progress, db_labels):
    result = {
        'create': create_anno_container(),
        'update': create_anno_container(),
        'delete': create_anno_container(),
    }

    data = get_image_data(path_to_data)
    data_len = len(data)
    model, class_names = load_model(model_file, weights_file, config_file)
    frame_counter = 0

    labels_mapping = {}
    for db_key, db_label in db_labels.items():
        for key, label in class_names.items():
            if label == db_label:
                labels_mapping[key] = db_key

    if not len(labels_mapping.values()):
        raise Exception('No labels found for annotation')

    detections = []
    for _, frame in data:
        model.setInput([frame])
        orig_rows, orig_cols = frame.shape[:2]

        detections.append({
            'frame_id': frame_counter,
            'frame_height': orig_rows,
            'frame_width': orig_cols,
            'detections': model.forward(),
        })

        frame_counter += 1
        if not update_progress(job, frame_counter * 100 / data_len):
            return None

    processed_detections = process_detections(detections, convertation_file)

    if 'boxes' in processed_detections:
        for box_ in processed_detections['boxes']:
            result['create']['boxes'].append({
                "label_id": labels_mapping[box_['label']],
                "frame": box_['frame'],
                "xtl": box_['xtl'],
                "ytl": box_['ytl'],
                "xbr": box_['xbr'],
                "ybr": box_['ybr'],
                "z_order": 0,
                "group_id": 0,
                "occluded": False,
                "attributes": [],
            })

    if 'box_path' in processed_detections:
        for box_path in processed_detections['box_path']:
            # TODO need implement
            pass

    return result

def update_progress(job, progress):
    job.refresh()
    if 'cancel' in job.meta:
        del job.meta['cancel']
        job.save()
        return False
    job.meta['progress'] = progress
    job.save_meta()
    return True

def create_thread(tid, db_labels, model_file, weights_file, config_file, convertation_file):
    try:
        job = rq.get_current_job()
        job.meta['progress'] = 0
        job.save_meta()
        db_task = TaskModel.objects.get(pk=tid)

        # Run auto annotation by tf
        result = None

        slogger.glob.info('custom annotation with openvino toolkit for task {}'.format(tid))
        result = run_inference_engine_annotation(
            path_to_data=db_task.get_data_dirname(),
            model_file=model_file,
            weights_file=weights_file,
            config_file=config_file,
            convertation_file= convertation_file,
            job=job,
            update_progress=update_progress,
            db_labels=db_labels,
        )

        if result is None:
            slogger.glob.info('custom annotation for task {} canceled by user'.format(tid))
            return

        annotation.clear_task(tid)
        annotation.save_task(tid, result)
        slogger.glob.info('custom annotation for task {} done'.format(tid))
    except:
        try:
            slogger.task[tid].exception('exception was occured during custom annotation of the task', exc_info=True)
        except:
            slogger.glob.exception('exception was occured during custom annotation of the task {}'.format(tid), exc_into=True)

@login_required
def get_meta_info(request):
    try:
        queue = django_rq.get_queue('low')
        tids = json.loads(request.body.decode('utf-8'))
        result = {}
        for tid in tids:
            job = queue.fetch_job('custom_annotation.create/{}'.format(tid))
            if job is not None:
                result[tid] = {
                    "active": job.is_queued or job.is_started,
                    "success": not job.is_failed
                }

        return JsonResponse(result)
    except Exception as ex:
        slogger.glob.exception('exception was occurred during custom annotation meta request', exc_into=True)
        return HttpResponseBadRequest(str(ex))

@login_required
@permission_required(perm=['engine.task.change'],
    fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def create(request, tid):
    slogger.glob.info('custom annotation create request for task {}'.format(tid))

    def write_file(path, file_obj):
        with open(path, 'wb') as upload_file:
            for chunk in file_obj.chunks():
                upload_file.write(chunk)

    try:
        db_task = TaskModel.objects.get(pk=tid)
        upload_dir = db_task.get_upload_dirname()
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('custom_annotation.create/{}'.format(tid))
        if job is not None and (job.is_started or job.is_queued):
            raise Exception("The process is already running")

        params = request.POST.dict()
        model_file = request.FILES['model']
        model_file_path = os.path.join(upload_dir, model_file.name)
        write_file(model_file_path, model_file)

        weights_file = request.FILES['weights']
        weights_file_path = os.path.join(upload_dir, weights_file.name)
        write_file(weights_file_path, weights_file)

        config_file = request.FILES['config']
        config_file_path = os.path.join(upload_dir, config_file.name)
        write_file(config_file_path, config_file)

        convertation_file = request.FILES['conv_script']
        convertation_file_path = os.path.join(upload_dir, convertation_file.name)
        write_file(convertation_file_path, convertation_file)


        db_labels = db_task.label_set.prefetch_related('attributespec_set').all()
        db_labels = {db_label.id:db_label.name for db_label in db_labels}

        # if not label_mapping:
        #     raise Exception('No labels found ')

        queue.enqueue_call(func=create_thread,
            args=(tid, db_labels, model_file_path, weights_file_path, config_file_path, convertation_file_path),
            job_id='custom_annotation.create/{}'.format(tid),
            timeout=604800)     # 7 days

        # slogger.task[tid].info('openVino annotation job enqueued with labels {}'.format(label_mapping))

    except Exception as ex:
        try:
            slogger.task[tid].exception("exception was occured during annotation request", exc_info=True)
        except:
            pass
        return HttpResponseBadRequest(str(ex))

    return HttpResponse()

@login_required
@permission_required(perm=['engine.task.access'],
    fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def check(request, tid):
    try:
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('custom_annotation.create/{}'.format(tid))
        if job is not None and 'cancel' in job.meta:
            return JsonResponse({'status': 'finished'})
        data = {}
        if job is None:
            data['status'] = 'unknown'
        elif job.is_queued:
            data['status'] = 'queued'
        elif job.is_started:
            data['status'] = 'started'
            data['progress'] = job.meta['progress']
        elif job.is_finished:
            data['status'] = 'finished'
            job.delete()
        else:
            data['status'] = 'failed'
            job.delete()

    except Exception:
        data['status'] = 'unknown'

    return JsonResponse(data)


@login_required
@permission_required(perm=['engine.task.change'],
    fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def cancel(request, tid):
    try:
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('custom_annotation.create/{}'.format(tid))
        if job is None or job.is_finished or job.is_failed:
            raise Exception('Task is not being annotated currently')
        elif 'cancel' not in job.meta:
            job.meta['cancel'] = True
            job.save()

    except Exception as ex:
        try:
            slogger.task[tid].exception("cannot cancel OpenVINO annotation for task #{}".format(tid), exc_info=True)
        except:
            pass
        return HttpResponseBadRequest(str(ex))

    return HttpResponse()
