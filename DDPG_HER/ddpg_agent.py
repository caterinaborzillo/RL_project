import torch
import os
from datetime import datetime
import numpy as np
from mpi4py import MPI
from from_baselines.mpi_utils import sync_networks, sync_grads
from DDPG_HER.replay_buffer import replay_buffer
from DDPG_HER.actor_critic import actor, critic
from from_baselines.normalizer import normalizer
from DDPG_HER.her import her_sampler

"""
ddpg algorithm + HER (with MPI)

"""
class ddpg_agent:
    def __init__(self, args, env, env_params):
        self.args = args
        self.env = env
        self.env_params = env_params
        # create the network
        self.actor_network = actor(env_params)
        self.critic_network = critic(env_params)
        # sync the networks across the cpus
        sync_networks(self.actor_network)
        sync_networks(self.critic_network)
        # build up the target network
        self.actor_target_network = actor(env_params)
        self.critic_target_network = critic(env_params)
        # load the weights into the target networks
        self.actor_target_network.load_state_dict(self.actor_network.state_dict())
        self.critic_target_network.load_state_dict(self.critic_network.state_dict())
        # if use gpu
        if self.args.cuda:
            self.actor_network.cuda() # it keeps track of the currently selected GPU, and all CUDA tensors you allocate will by default be created on that device. 
            self.critic_network.cuda()
            self.actor_target_network.cuda()
            self.critic_target_network.cuda()
        # create the optimizer
        self.actor_optim = torch.optim.Adam(self.actor_network.parameters(), lr=self.args.lr_actor)
        self.critic_optim = torch.optim.Adam(self.critic_network.parameters(), lr=self.args.lr_critic)
        # her sampler
        self.her_module = her_sampler(self.args.replay_strategy, self.args.replay_k, self.env.compute_reward)
        # create the replay buffer
        self.buffer = replay_buffer(self.env_params, self.args.buffer_size, self.her_module.sample_her_transitions)
        # create the normalizer
        self.o_norm = normalizer(size=env_params['obs'], default_clip_range=self.args.clip_range)
        self.g_norm = normalizer(size=env_params['goal'], default_clip_range=self.args.clip_range)
        # create the dict for store the model
        if MPI.COMM_WORLD.Get_rank() == 0:
            if not os.path.exists(self.args.save_dir):
                os.mkdir(self.args.save_dir)
            # path to save the model
            self.model_path = os.path.join(self.args.save_dir, self.args.env_name)
            if not os.path.exists(self.model_path):
                os.mkdir(self.model_path)

    def training(self):
        """
        train the network

        """
        to_plot = []
        # start to collect samples
        for epoch in range(self.args.n_epochs):
            for _ in range(self.args.n_cycles):
                mb_obs, mb_ag, mb_g, mb_actions = [], [], [], []
                for _ in range(self.args.num_rollouts_per_mpi): # assegnazione di tot episodi per ciascun thread
                    # reset the rollouts
                    ep_obs, ep_ag, ep_g, ep_actions = [], [], [], []
                    # reset the environment
                    observation = self.env.reset()
                    obs = observation['observation']
                    ag = observation['achieved_goal']
                    g = observation['desired_goal']
                    # start to collect samples
                    for t in range(self.env_params['max_timesteps']): # max_timesteps = max number of transitions per episode
                        with torch.no_grad(): # it just disables the tracking of any calculations required to later calculate a gradient 
                            input_tensor = self.input_preprocessing(obs, g) # input scaling 
                            pi = self.actor_network(input_tensor)       # ritorna tante azioni
                            action = self._choose_actions(pi)           # sceglie una sola azione randomicamente (exploration)
                        # feed the actions into the environment
                        observation_new, _, _, info = self.env.step(action)
                        obs_new = observation_new['observation']        # mi salvo il nuovo stato osservato nell'environment
                        ag_new = observation_new['achieved_goal']       # mi salvo il goal achieved in ag_new
                        # append rollouts
                        ep_obs.append(obs.copy())
                        ep_ag.append(ag.copy())
                        ep_g.append(g.copy())
                        ep_actions.append(action.copy())
                        # re-assign the observation
                        obs = obs_new
                        ag = ag_new
                    ep_obs.append(obs.copy())
                    ep_ag.append(ag.copy())
                    mb_obs.append(ep_obs)
                    mb_ag.append(ep_ag)
                    mb_g.append(ep_g)
                    mb_actions.append(ep_actions)
                # convert them into arrays
                mb_obs = np.array(mb_obs)
                mb_ag = np.array(mb_ag)
                mb_g = np.array(mb_g)
                mb_actions = np.array(mb_actions)
                # store the episodes in the replay buffer
                self.buffer.store_episode([mb_obs, mb_ag, mb_g, mb_actions])
                self._update_normalizer([mb_obs, mb_ag, mb_g, mb_actions])
                for _ in range(self.args.n_batches): # n_batches: the times to update the network
                    # train the network
                    self.network_updating()  # aggiorno i parametri della rete neurale (actor+critic)
                # soft update: per agiornare anche le le target networks
                # perchè soft? perchè in DDPG solo una parte dei parametri principali viene trasferita dalla nostra network alla target network
                self._soft_update_target_network(self.actor_target_network, self.actor_network)
                self._soft_update_target_network(self.critic_target_network, self.critic_network)
            # start to do the evaluation
            success_rate = self.evaluation()
            if MPI.COMM_WORLD.Get_rank() == 0:
                print('[{}] epoch is: {}, eval success rate is: {:.3f}'.format(datetime.now(), epoch, success_rate))
                to_plot.append(success_rate)
                torch.save([self.o_norm.mean, self.o_norm.std, self.g_norm.mean, self.g_norm.std, self.actor_network.state_dict()], \
                            self.model_path + '/model.pt')
        if MPI.COMM_WORLD.Get_rank() == 0:
            plt.plot(range(self.args.n_epochs), to_plot)
            
            plt.xlabel('Epoch')

            plt.ylabel('Mean Success Rate')
            
            plt.title("{} using DDPG + HER".format(self.args.env_name))
            plt.savefig("{}_DDPG_HER".format(self.args.env_name))

            plt.show()

    # pre_process the inputs
    def input_preprocessing(self, obs, g):
        obs_norm = self.o_norm.normalize(obs)
        g_norm = self.g_norm.normalize(g)
        # concatenate the stuffs
        inputs = np.concatenate([obs_norm, g_norm])
        inputs = torch.tensor(inputs, dtype=torch.float32).unsqueeze(0)
        if self.args.cuda:
            inputs = inputs.cuda()
        return inputs
    
    # this function will choose action for the agent and do the exploration or exploitation (scelgo azione data la policy)
    def _choose_actions(self, pi):
        action = pi.cpu().numpy().squeeze()
        # add the gaussian noise
        gaussian_noise = self.args.noise_eps * self.env_params['action_max'] * np.random.randn(*action.shape) #np.random.randn(*action.shape) -> randn generates an array of shape "action.shape", filled with random floats sampled from a univariate “normal” (Gaussian) distribution of mean 0 and variance 1
        action += gaussian_noise
        action = np.clip(action, -self.env_params['action_max'], self.env_params['action_max'])
        # random actions...
        random_actions = np.random.uniform(low=-self.env_params['action_max'], high=self.env_params['action_max'], size=self.env_params['action'])
        # choose if use the random actions (eps-greedy)
        eps_greedy_noise = np.random.binomial(1, self.args.random_eps, 1)[0] # restituisce solo o 1 (exploration) o 0 (exploitation)
        action += eps_greedy_noise * (random_actions - action)

        # random_eps = 0.3 
        # su 10 volte, 3 volte esce 1 e quindi prendo randomico e 7 volte esce 0 e quindi prendo la action secondo la policy
        # action = action + (random_actions - action) = random_actions (quando esp_greedy_noise è 1)

        return action

    # update the normalizer
    def _update_normalizer(self, episode_batch):
        mb_obs, mb_ag, mb_g, mb_actions = episode_batch
        mb_obs_next = mb_obs[:, 1:, :]
        mb_ag_next = mb_ag[:, 1:, :]
        # get the number of normalization transitions
        num_transitions = mb_actions.shape[1]
        # create the new buffer (in the shape of dictionary) to store thnormalizer em
        buffer_temp = {'obs': mb_obs, 
                       'ag': mb_ag,
                       'g': mb_g, 
                       'actions': mb_actions, 
                       'obs_next': mb_obs_next,
                       'ag_next': mb_ag_next,
                       }
        # sample a random minibatch of N transitions from R (both rewards 0 and 1) - see DDPG pseudo-code
        transitions = self.her_module.sample_her_transitions(buffer_temp, num_transitions) 
        obs, g = transitions['obs'], transitions['g']
        # pre process the obs and g
        transitions['obs'], transitions['g'] = self._preproc_og(obs, g)
        # update
        self.o_norm.update(transitions['obs'])
        self.g_norm.update(transitions['g'])
        # recompute the stats
        self.o_norm.recompute_stats()
        self.g_norm.recompute_stats()

    def _preproc_og(self, o, g):
        o = np.clip(o, -self.args.clip_obs, self.args.clip_obs)
        g = np.clip(g, -self.args.clip_obs, self.args.clip_obs)
        return o, g

    # soft update
    def _soft_update_target_network(self, target, source):
        for target_param, param in zip(target.parameters(), source.parameters()):
            target_param.data.copy_((1 - self.args.polyak) * param.data + self.args.polyak * target_param.data)

    # update the network
    def network_updating(self):
        # sample the episodes
        transitions = self.buffer.sample(self.args.batch_size)
        # pre-process the observation and goal
        o, o_next, g = transitions['obs'], transitions['obs_next'], transitions['g']
        transitions['obs'], transitions['g'] = self._preproc_og(o, g)
        transitions['obs_next'], transitions['g_next'] = self._preproc_og(o_next, g)
        # start to do the update
        obs_norm = self.o_norm.normalize(transitions['obs'])
        g_norm = self.g_norm.normalize(transitions['g'])
        inputs_norm = np.concatenate([obs_norm, g_norm], axis=1)
        obs_next_norm = self.o_norm.normalize(transitions['obs_next'])
        g_next_norm = self.g_norm.normalize(transitions['g_next'])
        inputs_next_norm = np.concatenate([obs_next_norm, g_next_norm], axis=1)
        # transfer them into the tensor for the neural networks
        inputs_norm_tensor = torch.tensor(inputs_norm, dtype=torch.float32)             # pytorch tensor with states seen so far
        inputs_next_norm_tensor = torch.tensor(inputs_next_norm, dtype=torch.float32)   # pytorch tensor with actions seen so far
        actions_tensor = torch.tensor(transitions['actions'], dtype=torch.float32)
        r_tensor = torch.tensor(transitions['r'], dtype=torch.float32) 
        if self.args.cuda:
            inputs_norm_tensor = inputs_norm_tensor.cuda()
            inputs_next_norm_tensor = inputs_next_norm_tensor.cuda()
            actions_tensor = actions_tensor.cuda()
            r_tensor = r_tensor.cuda()
        # calculate the target Q value function
        # torch.no_grad: it says that no operation should build the graph, no results are storage; it will use less memory because it knows from the beginning 
        # that no gradients are needed so it doesn’t need to keep intermediary results.
        with torch.no_grad():
            # do the normalization
            # concatenate the stuffs
            actions_next = self.actor_target_network(inputs_next_norm_tensor)
            q_next_value = self.critic_target_network(inputs_next_norm_tensor, actions_next)
            # detach(): creates a tensor that shares storage with tensor that does not require grad. It detaches the output from the computational graph, 
            # so no gradient will be backpropagated along this variable.
            q_next_value = q_next_value.detach() 
            target_q_value = r_tensor + self.args.gamma * q_next_value
            target_q_value = target_q_value.detach()
            # clip the q value
            clip_return = 1 / (1 - self.args.gamma) # we clip the targets used to train the critic to the range of possible values
            target_q_value = torch.clamp(target_q_value, -clip_return, 0) # clamp all elements in input into the range [ min, max ] and return a resulting tensor
        # the q loss
        real_q_value = self.critic_network(inputs_norm_tensor, actions_tensor)
        critic_loss = (target_q_value - real_q_value).pow(2).mean() # standard equation of the error function
        # the actor loss
        actions_real = self.actor_network(inputs_norm_tensor)
        # used "-value" as we want to maximize the value given by the critic for our actions
        actor_loss = -self.critic_network(inputs_norm_tensor, actions_real).mean() # massimizzare performance equivale a minimizzare l'errore
        # aggiungiamo il regularization term (action_l2 = regularization factor) 
        actor_loss += self.args.action_l2 * (actions_real / self.env_params['action_max']).pow(2).mean()

        # start to update the network
        self.actor_optim.zero_grad() #in PyTorch, we need to set the gradients to zero before starting to do backpropragation because PyTorch accumulates the gradients on subsequent backward passes.
        actor_loss.backward() # compute gradient of actor loss function through the whole actor network
        sync_grads(self.actor_network)
        self.actor_optim.step() # aggiorna i parametri dell'actor network, dopo aver calcolato il gradient with backward()
        # update the critic_network
        self.critic_optim.zero_grad() # set the gradients to zero
        critic_loss.backward()        # compute the gradient of the loss function della critic network
        sync_grads(self.critic_network) 
        self.critic_optim.step()      # aggiorna i parametri della critic network con adam optimizer

    # do the evaluation
    def evaluation(self):
        total_success_rate = []
        for _ in range(self.args.n_test_rollouts): # n_test_rollouts: the number of tests
            per_success_rate = []
            observation = self.env.reset()
            obs = observation['observation'] 
            g = observation['desired_goal']
            for _ in range(self.env_params['max_timesteps']): # timesteps in one episode t = 1..T
                with torch.no_grad():
                    input_tensor = self.input_preprocessing(obs, g) # normalize the input
                    pi = self.actor_network(input_tensor)
                    # convert the actions
                    # squeeze(): rimozione di una dimensione
                    actions = pi.detach().cpu().numpy().squeeze()   # prepara l'azione per essere eseguita
                observation_new, _, _, info = self.env.step(actions) # eseguo l'azione nell'ambiente e prendo il nuovo stato osservato + l'info riguardo al goal raggiunto
                obs = observation_new['observation'] # mi salvo il nuovo stato in 'obs'
                g = observation_new['desired_goal']
                per_success_rate.append(info['is_success'])
            total_success_rate.append(per_success_rate) # qui si segna per ogni timestep se lo stato ragiunto è il goal (ovviamente per gli stati intermedi, che non sono quindi goal, non ci sarà il successo). ad espisodio -> [0,0,0,1]
        total_success_rate = np.array(total_success_rate)
        local_success_rate = np.mean(total_success_rate[:, -1]) # considera solo se lo stato finale è il goal che voleva raggiungere (si prende solo l'ultima colonna)
        global_success_rate = MPI.COMM_WORLD.allreduce(local_success_rate, op=MPI.SUM)
        return global_success_rate / MPI.COMM_WORLD.Get_size()
