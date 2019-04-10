import numpy as np
import torch
from torch import nn, autograd
import gym
from copy import deepcopy
from collections import deque, OrderedDict
import os
import os.path as osp
import gtimer as gt
import torch.multiprocessing as mp
mp.set_sharing_strategy('file_system')
mp = mp.get_context('spawn')

from net import Net
from tools.utils import set_seed, zero_grad, loss_func


def _aggregate_rollout(rollout, state, action, next_state):
    '''
    add transition (state, action, next_state) to rollout
    rollout shape: [[s1, s2, ..., sn], [a1, a2, ..., an], [s'1, s'2, ..., s'n]]
    '''
    if rollout == []:
        rollout = [torch.stack((torch.tensor(state, dtype=torch.float),)),
            torch.stack((torch.tensor(action, dtype=torch.float),)),
            torch.stack((torch.tensor(next_state, dtype=torch.float),))]
    else:
        rollout = [torch.cat((rollout[0], torch.tensor(state, dtype=torch.float).view(1, -1))),
            torch.cat((rollout[1], torch.tensor(action, dtype=torch.float).view(1, -1))),
            torch.cat((rollout[2], torch.tensor(next_state, dtype=torch.float).view(1, -1)))]
    return rollout

def _collect_traj_per_thread(pid, queue, task, controller, theta, rollout_num, rollout_len, M,
    phi, adaptation_update_num, loss_type, loss_scale, pred_std):
    '''
    per thread function of parallel trajectory collection
    see MBMRL._collect_traj_serial() for method description
    '''
    rollouts = []
    state = task.reset()
    controller.set_task(task)
    _n_model_steps_total = 0
    _n_task_steps_total = 0

    for _ in range(rollout_num):
        rollout = []
        t = 0
        while t < rollout_len:
            past_traj = [r[-M:] for r in rollout]
            new_theta_dict = None
            if past_traj:
                st, ac, next_st = past_traj
                delta_st = theta(torch.cat((st, ac), 1), new_params=new_theta_dict)
                pred_next_st = st + delta_st
                loss = loss_scale * loss_func(loss_type, pred_next_st, next_st, std=pred_std)
                _n_model_steps_total += 1
                d_theta = autograd.grad(loss, theta.parameters())

                new_theta_dict = {key: val.clone() for key, val in theta.state_dict().items()}
                new_theta_params = OrderedDict()
                for (key, val), d in zip(theta.named_parameters(), d_theta):
                    new_theta_params[key] = val - phi * d
                    new_theta_dict[key] = new_theta_params[key]
                
                for _ in range(adaptation_update_num):
                    st, ac, next_st = past_traj
                    delta_st = theta(torch.cat((st, ac), 1), new_params=new_theta_dict)
                    pred_next_st = st + delta_st
                    new_loss = loss_scale * loss_func(loss_type, pred_next_st, next_st, std=pred_std)
                    _n_model_steps_total += 1
                    zero_grad(new_theta_params.values())
                    d_theta = autograd.grad(new_loss, new_theta_params.values(), create_graph=True)
                    for (key, val), d in zip(theta.named_parameters(), d_theta):
                        new_theta_params[key] = val - phi * d
                        new_theta_dict[key] = new_theta_params[key]

            action = controller.plan(theta, state, new_theta_dict)
            next_state, _, done, _ = task.step(action)
            if action.shape == ():
                action = [action]
            rollout = _aggregate_rollout(rollout, state, action, next_state)
            state = next_state
            t += 1
            _n_task_steps_total += 1
            if done:
                state = task.reset()
        rollouts.append(rollout)
    if queue is None:
        return rollouts, _n_model_steps_total, _n_task_steps_total
    else:
        queue.put([rollouts, _n_model_steps_total, _n_task_steps_total])

def _evaluate_per_thread(queue, tasks, controller, theta):
    '''
    per thread function of reward evaluation
    see MBMRL._evaluate_serial() for method description
    '''
    rewards = []
    for task in tasks:
        controller.set_task(task)
        state = task.reset()
        done = False
        reward_sum = 0
        while not done:
            action = controller.plan(theta, state)
            state, reward, done, _ = task.step(action)
            reward_sum += reward
        rewards.append(reward_sum)
    
    if queue is None:
        return rewards
    else:
        queue.put(rewards)


class MBMRL:
    ''' 
    Model-Based Meta-Reinforcement Learning
    Original Paper: Nagabandi et al., 2019, 'Learning to Adapt in Dynamic, Real-World Environments through Meta-Reinforcement Learning'
    '''
    def __init__(self, tasks, model, controller, logger, seed, iteration_num, task_sample_num, task_sample_frequency, eval_frequency, eval_sample_num,
            rollout_len, rollout_num, adaptation_update_num, M, K, beta, eta, phi, dataset_size, pred_std, loss_type, loss_scale, num_threads):
        set_seed(seed)
        self.logger = logger

        self.tasks = np.array(tasks)    # training tasks
        self.controller = controller    # MPC controller
        self.dataset = deque(maxlen=int(dataset_size))  # dataset D storing rollouts containing (s, a, s') transition

        self.iteration_num = int(iteration_num) # number of total training iteration
        self.task_sample_num = int(task_sample_num) # number of sampled tasks N
        self.task_sample_frequency = int(task_sample_frequency) # tasks sampling frequency n_s
        self.eval_frequency = int(eval_frequency)   # reward evaluation frequency
        self.eval_sample_num = int(eval_sample_num) # number of samples for evaluation
        self.rollout_len = int(rollout_len) # max length of rollout in trajectory collection, i.e., number of (s, a, s') transition in a single rollout
        self.rollout_num = int(rollout_num) # number of rollouts for once trajectory collection
        self.adaptation_update_num = int(adaptation_update_num) # number of updates per adaptation
        self.M = M  # number of previous datapoints
        self.K = K  # number of future datapoints
        self.beta = beta    # meta model learning rate
        self.eta = eta  # learning rate of update rule of adaptation (phi)
        self.pred_std = pred_std    # std for use NLL loss (TODO: make it learnable)
        self.loss_type = loss_type  # type of loss between real and predicted value of next state
        self.loss_scale = loss_scale    # scaling factor of loss
        self.num_threads = num_threads  # number of threads for parallization

        self.theta = model  # dynamics neural network model
        self.meta_optimizer = torch.optim.Adam(self.theta.parameters(), lr=self.beta)   # optimizer for dynamics
        self.phi = torch.tensor(phi, requires_grad=True)    # update rule of adaptation (a learning rate in GrBAL)
        self.lr_optimizer = torch.optim.Adam([self.phi], lr=self.eta)   # optimizer for update rule of adaptation

        self.theta_loss = None
        self.eval_rewards = []
        self._epoch_start_time = None
        self._n_task_steps_total = 0
        self._n_model_steps_total = 0
        self._n_rollouts_total = 0
        self._time_total = 0
        self._time_total_prev = 0

    ##### LOGGER #####

    def _get_params(self, iter):
        return {
            'iteration': iter,
            'theta': self.theta.state_dict(),
            'meta_optimizer': self.meta_optimizer.state_dict(),
            'lr_optimizer': self.lr_optimizer.state_dict(),
            'phi': self.phi,
            'loss': self.theta_loss,
            }
    
    def _set_params(self, params):
        self.theta.load_state_dict(params['theta'])
        self.meta_optimizer.load_state_dict(params['meta_optimizer'])
        self.lr_optimizer.load_state_dict(params['lr_optimizer'])
        self.phi = params['phi']
        self.theta_loss = params['loss']
        return params['iteration']
        
    def _get_extra_data(self):
        return {
            'dataset': self.dataset,
            }
    
    def _set_extra_data(self, extra_data):
        self.dataset = extra_data['dataset']

    def _get_stats(self):
        return {
            'n_task_steps': self._n_task_steps_total,
            'n_model_steps': self._n_model_steps_total,
            'n_rollouts': self._n_rollouts_total,
            'time': self._time_total,
            }

    def _set_stats(self, stats):
        self._n_task_steps_total = stats['n_task_steps']
        self._n_model_steps_total = stats['n_model_steps']
        self._n_rollouts_total = stats['n_rollouts']
        self._time_total_prev = stats['time']

    def _start_iteration(self, iter):
        self.logger.push_prefix('Iteration #%d | ' % iter)
        self.logger.record_tabular('Iteration', iter)

    def _end_iteration(self, iter):
        self._record_stats()
        self.logger.pop_prefix()
        params_to_be_saved = self._get_params(iter)
        params_to_be_saved.update(self._get_stats())
        self.logger.save_params(iter, params_to_be_saved)
        self.logger.save_extra_data(self._get_extra_data())

    def _record_stats(self):
        times_itrs = gt.get_times().stamps.itrs
        sample_time = times_itrs['sample'][-1]
        adaptation_time = times_itrs['adaptation'][-1]
        meta_time = times_itrs['meta'][-1]
        eval_time = times_itrs['eval'][-1]
        iter_time = sample_time + adaptation_time + meta_time + eval_time
        self._time_total = gt.get_times().total + self._time_total_prev

        self.logger.record_tabular('Model Loss', np.float32(self.theta_loss.data))
        for task, reward in zip(self.tasks, self.eval_rewards):
            self.logger.record_tabular('Reward: ' + task.env.spec.id, reward)
        self.logger.record_tabular('Dataset Size', len(self.dataset))
        self.logger.record_tabular('Total Model Steps', self._n_model_steps_total)
        self.logger.record_tabular('Total Task Steps', self._n_task_steps_total)
        self.logger.record_tabular('Total Rollouts', self._n_rollouts_total)
        self.logger.record_tabular('Sample Time (s)', sample_time)
        self.logger.record_tabular('Adaptation Time (s)', adaptation_time)
        self.logger.record_tabular('Meta Time (s)', meta_time)
        self.logger.record_tabular('Evaluation Time (s)', eval_time)
        self.logger.record_tabular('Iteration Time (s)', iter_time)
        self.logger.record_tabular('Total Time (s)', self._time_total)
        
        self.logger.dump_tabular(with_prefix=False, with_timestamp=False)

     ##### ALGORITHM #####

    def _compute_adaptation_loss(self, theta, traj, new_theta=None):
        state, action, next_state = traj
        delta_state = theta(torch.cat((state, action), 1), new_params=new_theta)
        pred_next_state = state + delta_state
        loss = self.loss_scale / len(state) * loss_func(self.loss_type, pred_next_state, next_state, std=self.pred_std)
        self._n_model_steps_total += 1
        return loss

    def _adaptation_update(self, theta, traj):
        if traj == []:
            return None

        loss = self._compute_adaptation_loss(theta, traj)
        d_theta = autograd.grad(loss, theta.parameters())

        new_theta_dict = {key: val.clone() for key, val in theta.state_dict().items()}
        new_theta_params = OrderedDict()
        for (key, val), d in zip(theta.named_parameters(), d_theta):
            new_theta_params[key] = val - self.phi * d
            new_theta_dict[key] = new_theta_params[key]
        
        for _ in range(self.adaptation_update_num):
            new_loss = self._compute_adaptation_loss(theta, traj, new_theta_dict)
            zero_grad(new_theta_params.values())
            d_theta = autograd.grad(new_loss, new_theta_params.values(), create_graph=True)
            for (key, val), d in zip(theta.named_parameters(), d_theta):
                new_theta_params[key] = val - self.phi * d
                new_theta_dict[key] = new_theta_params[key]

        return new_theta_dict

    def _meta_update(self, meta_loss):
        self.meta_optimizer.zero_grad()
        self.lr_optimizer.zero_grad()
        meta_loss.backward(retain_graph=True)
        self.meta_optimizer.step()
        self.lr_optimizer.step()

    def _collect_traj_parallel(self, task):
        workers = []
        queue = mp.Queue()
        rollout_nums = np.full(self.num_threads, self.rollout_num // self.num_threads, dtype=np.int)
        rollout_nums[:self.rollout_num % self.num_threads] += 1
        for pid, rollout_num_per_thread in zip(range(self.num_threads), rollout_nums):
            if rollout_num_per_thread > 0:
                worker_args = (pid, queue, task, self.controller, self.theta, rollout_num_per_thread, self.rollout_len, self.M,
                    self.phi, self.adaptation_update_num, self.loss_type, self.loss_scale, self.pred_std)
                workers.append(mp.Process(target=_collect_traj_per_thread, args=worker_args))
        for worker in workers:
            worker.start()
        
        rollouts = []
        for _ in workers:
            rollouts_per_thread, _n_model_steps_total, _n_task_steps_total = queue.get()
            rollouts.extend(rollouts_per_thread)
            self._n_model_steps_total += _n_model_steps_total
            self._n_task_steps_total += _n_task_steps_total
        return rollouts

    def _collect_traj_serial(self, task):
        rollouts = []
        self.controller.set_task(task)
        state = task.reset()
        for _ in range(self.rollout_num):
            rollout = []
            t = 0
            while t < self.rollout_len:
                past_traj = [r[-self.M:] for r in rollout]
                new_theta_dict = self._adaptation_update(self.theta, past_traj)
                action = self.controller.plan(self.theta, state, new_theta_dict)
                next_state, _, done, _ = task.step(action)
                if action.shape == ():
                    action = [action]
                rollout = _aggregate_rollout(rollout, state, action, next_state)
                state = next_state
                t += 1
                self._n_task_steps_total += 1
                if done:
                    state = task.reset()
            rollouts.append(rollout)
        return rollouts

    def _collect_traj(self, task):
        if self.num_threads > 1:
            return self._collect_traj_parallel(task)
        else:
            return self._collect_traj_serial(task)

    def _sample_traj(self, num=1):
        trajs = []
        for _ in range(num):
            rollout = self.dataset[np.random.choice(len(self.dataset))]
            start_idx = np.random.choice(len(rollout[0]) + 1 - self.M - self.K)
            end_idx = start_idx + self.M + self.K
            traj = [r[start_idx: end_idx] for r in rollout]
            if trajs == []:
                trajs = traj
            else:
                trajs = [torch.cat((trajs[0], traj[0])),
                    torch.cat((trajs[1], traj[1])),
                    torch.cat((trajs[2], traj[2]))]
        return trajs

    def _sample_task(self):
        # TODO: support task distribution
        task = np.random.choice(self.tasks)
        return task

    def _resume_train(self, load_iter):
        params = self.logger.load_params(load_iter)
        start_iter = self._set_params(params)
        self._set_stats(params)
        self.theta.train()
        extra_data = self.logger.load_extra_data()
        self._set_extra_data(extra_data)
        return start_iter

    def _evaluate_parallel(self):
        workers = []
        queue = mp.Queue()
        for _ in range(self.eval_sample_num):
            worker_args = (queue, self.tasks, self.controller, self.theta)
            workers.append(mp.Process(target=_evaluate_per_thread, args=worker_args))
        for worker in workers:
            worker.start()

        mean_rewards = []
        for _ in workers:
            rewards = queue.get()
            mean_rewards.append(rewards)
        mean_rewards = np.mean(mean_rewards, axis=1)
        return mean_rewards

    def _evaluate_serial(self):
        mean_rewards = []
        for task in self.tasks:
            rewards = []
            self.controller.set_task(task)
            for _ in range(5):
                state = task.reset()
                done = False
                reward_sum = 0
                while not done:
                    action = self.controller.plan(self.theta, state)
                    state, reward, done, _ = task.step(action)
                    reward_sum += reward
                rewards.append(reward_sum)
            mean_rewards.append(np.mean(rewards))
        return mean_rewards

    def evaluate(self):
        if self.num_threads > 1 and self.eval_sample_num > 1:
            mean_rewards = self._evaluate_parallel()
        else:
            mean_rewards = self._evaluate_serial()
        self.eval_rewards = mean_rewards
        return mean_rewards

    def debug(self):
        for i in range(self.iteration_num):
            print('Iteration ' + str(i))
            if i % self.task_sample_frequency == 0:
                task = self._sample_task()
                rollouts = self._collect_traj(task)
                self.dataset.extend(rollouts)
                np.random.shuffle(self.dataset)
            new_losses = []
            for _ in range(self.task_sample_num):
                traj = self._sample_traj()
                new_theta_dict = self._adaptation_update(self.theta, [t[:self.M] for t in traj])
                new_loss = self._compute_adaptation_loss(self.theta, [t[:self.M] for t in traj], new_theta_dict)
                new_losses.append(new_loss)
            self.theta_loss = torch.mean(torch.stack(new_losses))
            self._meta_update(self.theta_loss)
            if i % self.eval_frequency == 0:
                mean_rewards = self.evaluate()
                for task, mean_reward in zip(self.tasks, mean_rewards):
                    print(task.env.spec.id + ' Reward: ' + str(mean_reward))

    def train(self, resume=False, load_iter=None):
        gt.reset()
        gt.set_def_unique(False)
        start_iter = self._resume_train(load_iter) if resume else 0

        for i in gt.timed_for(range(start_iter, self.iteration_num), save_itrs=True):
            self._start_iteration(i)

            if i % self.task_sample_frequency == 0:
                self.logger.log('Data Collection')
                task = self._sample_task()
                # collect trajectories on sampled tasks and aggregate to dataset
                rollouts = self._collect_traj(task)
                self._n_rollouts_total += 1
                self.dataset.extend(rollouts)
                np.random.shuffle(self.dataset)
            gt.stamp('sample')

            self.logger.log('Adaptation Update')
            new_losses = []
            for j in range(self.task_sample_num):
                # sample M+K steps from dataset
                traj = self._sample_traj()
                # do adaptation update, get new theta and gradients
                new_theta_dict = self._adaptation_update(self.theta, [t[:self.M] for t in traj])
                # compute loss using new theta
                new_loss = self._compute_adaptation_loss(self.theta, [t[:self.M] for t in traj], new_theta_dict)
                new_losses.append(new_loss)
            self.theta_loss = torch.mean(torch.stack(new_losses))
            gt.stamp('adaptation')

            # do meta update on theta and phi
            self.logger.log('Meta Update')
            self._meta_update(self.theta_loss)
            gt.stamp('meta')

            if i % self.eval_frequency == 0:
                self.logger.log('Evaluation')
                mean_rewards = self.evaluate()
                for task, mean_reward in zip(self.tasks, mean_rewards):
                    self.logger.log(task.env.spec.id + ' Reward: ' + str(mean_reward))
            gt.stamp('eval')

            self._end_iteration(i)

    def test(self, task, seed, iteration_num, render, load_iter=None):
        set_seed(seed)
        iteration_num = int(iteration_num)

        gt.reset()
        gt.set_def_unique(False)
        start_iter = 0

        params = self.logger.load_params(load_iter)
        start_iter = self._set_params(params)
        self.theta.train()
        extra_data = self.logger.load_extra_data()
        self._set_extra_data(extra_data)

        rollout = []
        state = task.reset()
        self.controller.set_task(task)
            
        for i in gt.timed_for(range(start_iter, iteration_num), save_itrs=True):

            t = 0
            done = False
            reward_sum = 0
            state = task.reset()
            while not done:
                past_traj = rollout[-self.M:]
                new_theta_dict = self._adaptation_update(self.theta, past_traj)
                action = self.controller.plan(self.theta, state, new_theta_dict)
                next_state, reward, done, _ = task.step(action)
                reward_sum += reward
                if new_theta_dict is not None:
                    self.theta.load_state_dict(new_theta_dict)
                if render:
                    task.render()

                if action.shape == ():
                    action = [action]
                
                rollout = _aggregate_rollout(rollout, state, action, next_state)
                state = next_state
                t += 1
                
                if done:
                    rollout = []
                    state = task.reset()

            print('Iteration:', i, 'Reward:', reward_sum, 'Traj len:', t)
