from collections import deque

import easy_tf_log
import numpy as np

import utils
from multi_scope_train_op import *
from network import create_network
from params import DISCOUNT_FACTOR


class Worker:

    def __init__(self, sess, env, worker_n, log_dir, debug, optimizer):
        self.sess = sess
        self.env = env
        self.worker_n = worker_n

        worker_name = "worker_{}".format(worker_n)
        self.network = create_network(scope=worker_name, debug=debug,
                                      n_actions=env.action_space.n)

        self.summary_writer = tf.summary.FileWriter(log_dir, flush_secs=1)
        self.logger = easy_tf_log.Logger()
        self.logger.set_writer(self.summary_writer.event_writer)

        self.train_op, grads_norm = create_train_op(
            self.network.loss,
            optimizer,
            compute_scope=worker_name,
            apply_scope='global',
            max_grad_norm=0.5)

        self.summaries_op = self.make_summaries_op(self.network, grads_norm,
                                                   optimizer, worker_name)

        self.copy_ops = utils.create_copy_ops(from_scope='global',
                                              to_scope=worker_name)

        self.render = False
        self.value_log = deque(maxlen=100)
        self.fig = None

        self.updates = 0
        self.last_o = self.env.reset()
        self.episode_values = []

    @staticmethod
    def make_summaries_op(network, grads_norm, optimizer, worker_name):
        grads_norm_policy = tf.global_norm(
            tf.gradients(network.policy_loss, tf.trainable_variables()))
        grads_norm_value = tf.global_norm(
            tf.gradients(network.value_loss, tf.trainable_variables()))
        summary_pairs = [
            ('rl/value_loss', network.value_loss),
            ('rl/policy_loss', network.policy_loss),
            ('rl/combined_loss', network.loss),
            ('rl/policy_entropy', network.policy_entropy),
            ('rl/advantage_mean', tf.reduce_mean(network.advantage)),
            ('gradients/norm', grads_norm),
            ('gradients/norm_policy', grads_norm_policy),
            ('gradients/norm_value', grads_norm_value),
        ]
        summaries = []
        for name, val in summary_pairs:
            full_name = "{}/{}".format(worker_name, name)
            summary = tf.summary.scalar(full_name, val)
            summaries.append(summary)

        rmsprop_summaries = utils.make_rmsprop_monitoring_ops(optimizer,
                                                              worker_name)
        summaries.extend(rmsprop_summaries)

        return tf.summary.merge(summaries)

    def value_graph(self):
        import matplotlib.pyplot as plt
        if self.fig is None:
            self.fig, self.ax = plt.subplots()
            self.fig.set_size_inches(2, 2)
            self.ax.set_xlim([0, 100])
            self.ax.set_ylim([0, 2.0])
            self.line, = self.ax.plot([], [])

            self.fig.show()
            self.fig.canvas.draw()
            self.bg = self.fig.canvas.copy_from_bbox(self.ax.bbox)

        self.fig.canvas.restore_region(self.bg)

        ydata = list(self.value_log)
        xdata = list(range(len(self.value_log)))
        self.line.set_data(xdata, ydata)

        self.ax.draw_artist(self.line)
        self.fig.canvas.update()
        self.fig.canvas.flush_events()

    def logkv(self, key, value):
        self.logger.logkv("worker_{}/".format(self.worker_n) + key, value)

    def run_update(self, n_steps):
        states = []
        actions = []
        rewards = []

        self.sess.run(self.copy_ops)

        for _ in range(n_steps):
            s = np.moveaxis(self.last_o, source=0, destination=-1)
            feed_dict = {self.network.s: [s]}
            [a_p], [v] = self.sess.run([self.network.a_softmax,
                                        self.network.graph_v],
                                       feed_dict=feed_dict)
            a = np.random.choice(self.env.action_space.n, p=a_p)
            self.episode_values.append(v)

            self.last_o, r, done, _ = self.env.step(a)

            # The state used to choose the action.
            # Not the current state. The previous state.
            states.append(np.copy(s))
            actions.append(a)
            rewards.append(r)

            if self.render:
                self.env.render()
                self.value_log.append(v)
                self.value_graph()

            if done:
                break

        # TODO gut more thoroughly
        #self.logkv('rl/batch_reward_sum', sum(rewards))

        last_state = np.copy(self.last_o)

        if done:
            returns = utils.rewards_to_discounted_returns(rewards,
                                                          DISCOUNT_FACTOR)
            self.last_o = self.env.reset()
            episode_value_sum = sum(self.episode_values)
            episode_value_mean = episode_value_sum / len(self.episode_values)
            self.logkv('rl/episode_value_sum', episode_value_sum)
            self.logkv('rl/episode_value_mean', episode_value_mean)
            self.episode_values = []
        else:
            # If we're ending in a non-terminal state, in order to calculate
            # returns, we need to know the return of the final state.
            # We estimate this using the value network.
            s = np.moveaxis(last_state, source=0, destination=-1)
            feed_dict = {self.network.s: [s]}
            last_value = self.sess.run(self.network.graph_v,
                                       feed_dict=feed_dict)[0]
            rewards += [last_value]
            returns = utils.rewards_to_discounted_returns(rewards,
                                                          DISCOUNT_FACTOR)
            returns = returns[:-1]  # Chop off last_value

        feed_dict = {self.network.s: states,
                     self.network.a: actions,
                     self.network.r: returns}
        self.sess.run(self.train_op, feed_dict)
        if self.updates != 0 and self.updates % 100 == 0:
            summaries = self.sess.run(self.summaries_op, feed_dict)
            self.summary_writer.add_summary(summaries, self.updates)

        self.updates += 1

        return len(states)
