#!/usr/bin/env python3

import argparse
import os
import os.path as osp
import time
from multiprocessing import Process

import easy_tf_log
import tensorflow as tf

from network import create_network
from utils import get_port_range, MemoryProfiler, get_git_rev
from worker import Worker

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'  # filter out INFO messages


def run_worker(env_id, worker_n, n_steps_to_run, ckpt_freq, load_ckpt_file,
               render, log_dir):
    mem_log = osp.join(log_dir, "worker_{}_memory.log".format(worker_n))
    memory_profiler = MemoryProfiler(pid=-1, log_path=mem_log)
    memory_profiler.start()

    worker_log_dir = osp.join(log_dir, "worker_{}".format(worker_n))
    easy_tf_log.set_dir(worker_log_dir)

    tensorflow_log_dir = osp.join(worker_log_dir, 'tensorflow')
    os.makedirs(tensorflow_log_dir)
    summary_writer = tf.summary.FileWriter(tensorflow_log_dir, flush_secs=1)

    server = tf.train.Server(cluster, job_name="worker", task_index=worker_n)
    sess = tf.Session(server.target)

    with tf.device("/job:worker/task:0"):
        create_network('global')
    with tf.device("/job:worker/task:%d" % worker_n):
        w = Worker(sess, worker_n, env_id, summary_writer)
        if render:
            w.render = True

    if worker_n == 0:
        saver = tf.train.Saver()
        checkpoint_file = os.path.join('checkpoints', 'network.ckpt')

    print("Waiting for cluster connection...")
    sess.run(tf.global_variables_initializer())

    if load_ckpt_file is not None:
        print("Restoring from checkpoint '%s'..." % load_ckpt_file,
              end='', flush=True)
        saver.restore(sess, load_ckpt_file)
        print("done!")

    print("Cluster established!")
    updates = 0
    steps = 0
    while steps < n_steps_to_run:
        start_time = time.time()

        steps_ran, done = w.run_update()
        steps += steps_ran
        updates += 1

        end_time = time.time()
        steps_per_second = steps_ran / (end_time - start_time)
        easy_tf_log.tflog('steps_per_second', steps_per_second)

        if done:
            w.reset_env()
        if worker_n == 0 and updates % ckpt_freq == 0:
            saver.save(sess, checkpoint_file)
            print("Checkpoint saved to '{}'".format(checkpoint_file))

    memory_profiler.stop()


parser = argparse.ArgumentParser()
parser.add_argument("env_id")
parser.add_argument("--n_steps", type=int, default=10)
parser.add_argument("--n_workers", type=int, default=16)
parser.add_argument("--ckpt_freq", type=int, default=500)
parser.add_argument("--load_ckpt")
parser.add_argument("--render", action='store_true')
group = parser.add_mutually_exclusive_group()
group.add_argument('--log_dir')
seconds_since_epoch = str(int(time.time()))
group.add_argument('--run_name', default=seconds_since_epoch)
args = parser.parse_args()

if args.log_dir:
    log_dir = args.log_dir
else:
    git_rev = get_git_rev()
    run_name = args.run_name + '_' + git_rev
    log_dir = osp.join('runs', run_name)
    if osp.exists(log_dir):
        raise Exception("Log directory '%s' already exists" % log_dir)
os.makedirs(log_dir, exist_ok=True)

if "MovingDot" in args.env_id:
    import gym_moving_dot

    gym_moving_dot  # TODO prevent PyCharm from removing the import

cluster_dict = {}
ports = get_port_range(start_port=2200, n_ports=args.n_workers)
cluster_dict["worker"] = ["localhost:{}".format(port)
                          for port in ports]
cluster = tf.train.ClusterSpec(cluster_dict)


def start_worker_process(worker_n):
    print("Starting worker", worker_n)
    run_worker(env_id=args.env_id,
               worker_n=worker_n,
               n_steps_to_run=args.n_steps,
               ckpt_freq=args.ckpt_freq,
               load_ckpt_file=args.load_ckpt,
               render=args.render,
               log_dir=log_dir)


worker_processes = []
memory_profiler_processes = []
for worker_n in range(args.n_workers):
    p = Process(target=start_worker_process, args=(worker_n,), daemon=True)
    p.start()
    worker_processes.append(p)

for p in worker_processes:
    p.join()
