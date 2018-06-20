import abc
from collections import OrderedDict

import mujoco_py
import numpy as np
import sys
from multiworld.envs.mujoco.mujoco_env import MujocoEnv
from gym.spaces import Box
from multiworld.core.serializable import Serializable
from multiworld.envs.env_util import get_stat_in_paths, \
    create_stats_ordered_dict, get_asset_full_path
from multiworld.core.multitask_env import MultitaskEnv
from railrl.exploration_strategies.base import PolicyWrappedWithExplorationStrategy
from railrl.exploration_strategies.epsilon_greedy import EpsilonGreedy
from railrl.exploration_strategies.ou_strategy import OUStrategy
from railrl.policies.simple import ZeroPolicy


class SawyerDoorEnv(MultitaskEnv, MujocoEnv, Serializable, metaclass=abc.ABCMeta):
	#TODO: MAKE THIS A SUBCLASS OF SAWYER MOCAP BASE
	def __init__(self,
					 frame_skip=30,
					 goal_low=-1.5708,
					 goal_high=1.5708,
					 pos_action_scale=1 / 100,
					 action_reward_scale=0,
					 reward_type='angle_difference',
					 indicator_threshold=0.02, #about 1 degree
					 fix_goal=False,
					 fixed_goal=(0.15, 0.6, 0.3),
				):
		self.quick_init(locals())
		MultitaskEnv.__init__(self)
		MujocoEnv.__init__(self, self.model_path, frame_skip=frame_skip)

		self.reward_type = reward_type
		self.indicator_threshold = indicator_threshold

		self.fix_goal = fix_goal
		self.fixed_goal = np.array(fixed_goal)
		self.goal_space = Box(np.array([goal_low]), np.array([goal_high]))
		self._state_goal = None

		self.action_space = Box(np.array([-1, -1, -1, -1]), np.array([1, 1, 1, 1]))
		max_angle = 1.5708
		self.state_space = Box(
			np.array([-1, -1, -1, -max_angle]),
			np.array([1, 1, 1, max_angle]),
		)

		self.observation_space = Dict([
			('observation', self.state_space),
			('desired_goal', self.state_space),
			('achieved_goal', self.state_space),
			('state_observation', self.state_space),
			('state_desired_goal', self.state_space),
			('state_achieved_goal', self.state_space),
		])
		self._pos_action_scale = pos_action_scale
		self.action_reward_scale = action_reward_scale

		self.reset()
		self.reset_mocap_welds()

	@property
	def model_path(self):
		return get_asset_full_path('sawyer_door/sawyer_door.xml')

	def reset_mocap_welds(self):
		"""Resets the mocap welds that we use for actuation."""
		sim = self.sim
		if sim.model.nmocap > 0 and sim.model.eq_data is not None:
			for i in range(sim.model.eq_data.shape[0]):
				if sim.model.eq_type[i] == mujoco_py.const.EQ_WELD:
					sim.model.eq_data[i, :] = np.array(
						[0., 0., 0., 1., 0., 0., 0.])
		sim.forward()

	def step(self, a):
		a = np.clip(a, -1, 1)
		self.mocap_set_action(a[:3] * self._pos_action_scale)
		u = np.zeros((7))
		self.do_simulation(u, self.frame_skip)
		info = self._get_info()
		obs = self._get_obs()
		reward = self.compute_reward(
			obs['achieved_goal'],
			obs['desired_goal'],
			info,
		)
		done = False
		return obs, reward, done, info

	def _get_obs(self):
		pos = self.get_endeff_pos()
		angle = self.get_door_angle()
		flat_obs = np.concatenate((pos, angle))
		return dict(
			observation=flat_obs,
			desired_goal=self._goal_angle,
			achieved_goal=angle,
			state_observation=flat_obs,
			state_desired_goal=self._goal_angle,
			state_achieved_goal=angle,
		)

	def _get_info(self):
		angle_diff = np.abs(self.get_door_angle()-self._goal_angle)
		info = dict(
			angle_difference=angle_diff,
			angle_success = (angle_diff < self.indicator_threshold).astype(float)
		)
		return info

	def mocap_set_action(self, action):
		pos_delta = action[None]
		self.reset_mocap2body_xpos()
		new_mocap_pos = self.data.mocap_pos + pos_delta
		new_mocap_pos[0, 2] = np.clip(
			0.06,
			0,
			0.5,
		)
		self.data.set_mocap_pos('mocap', new_mocap_pos)
		self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))

	def reset_mocap2body_xpos(self):
		self.data.set_mocap_pos(
			'mocap',
			np.array([self.data.body_xpos[self.endeff_id]]),
		)
		self.data.set_mocap_quat(
			'mocap',
			np.array([self.data.body_xquat[self.endeff_id]]),
		)

	def get_endeff_pos(self):
		return self.data.body_xpos[self.endeff_id].copy()

	def get_door_angle(self):
		return np.array([self.data.get_joint_qpos('doorjoint')])

	@property
	def endeff_id(self):
		return self.model.body_names.index('leftclaw')

	def _compute_reward(self, obs, goal, action):
		actual_angle = self.convert_ob_to_goal(obs)
		return - np.abs(actual_angle - goal)[0] - np.linalg.norm(action) * self.action_reward_scale

	def compute_rewards(self, achieved_goals, desired_goals, info):
		r = np.abs(achieved_goals-desired_goals)
		if self.reward_type == 'angle_difference':
			r =  -r
		elif self.reward_type == 'hand_success':
			r = -(r < self.indicator_threshold).astype(float)
		else:
			raise NotImplementedError("Invalid/no reward type.")
		return r

	def reset(self, resample_on_reset=True):
		angles = self.data.qpos.copy()
		velocities = self.data.qvel.copy()
		angles[:] = self.init_angles
		velocities[:] = 0
		self.set_state(angles.flatten(), velocities.flatten())
		if resample_on_reset:
			goal = self.sample_goal()
			self._goal_angle = goal['state_desired_goal']
		return self._get_obs()

	@property
	def init_angles(self):
		return [
			0,
			1.02866769e+00, - 6.95207647e-01, 4.22932911e-01,
			1.76670458e+00, - 5.69637604e-01, 6.24117280e-01,
			3.53404635e+00,
		]

	''' Multitask Functions '''

	@property
	def goal_dim(self):
		return 1

	def sample_goals(self, batch_size):
		if self.fix_goal:
			goals = np.repeat(
				self.fixed_goal.copy()[None],
				batch_size,
				0
			)
		else:
			goals = np.random.uniform(
				self.goal_space.low,
				self.goal_space.high,
				size=(batch_size, self.goal_space.low.size),
			)
		return {
			'desired_goal': goals,
			'state_desired_goal': goals,
		}

	def set_goal_angle(self, angle):
		self._goal_angle = angle.copy()
		qpos = self.data.qpos.flat.copy()
		qvel = self.data.qvel.flat.copy()
		qpos[0] = angle.copy()
		qvel[0] = 0
		self.set_state(qpos, qvel)

	def set_goal_pos(self, xyz):
		for _ in range(10):
			self.data.set_mocap_pos('mocap', np.array(xyz))
			self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
			u = np.zeros(7)
			self.do_simulation(u, self.frame_skip)

	def get_goal(self):
		return {
			'desired_goal': self._goal_angle,
			'state_desired_goal': self._goal_angle,
		}

	def set_to_goal(self, goal):
		state_goal = goal['state_desired_goal']
		self.set_goal_angle(state_goal)

	def get_diagnostics(self, paths, prefix=''):
		statistics = OrderedDict()
		for stat_name in [
			'angle_difference',
			'angle_success',
		]:
			stat_name = stat_name
			stat = get_stat_in_paths(paths, 'env_infos', stat_name)
			statistics.update(create_stats_ordered_dict(
				'%s%s' % (prefix, stat_name),
				stat,
				always_show_all_stats=True,
			))
			statistics.update(create_stats_ordered_dict(
				'Final %s%s' % (prefix, stat_name),
				[s[-1] for s in stat],
				always_show_all_stats=True,
			))
		return statistics

	def get_env_state(self):
		joint_state = self.sim.get_state()
		mocap_state = self.data.mocap_pos, self.data.mocap_quat
		base_state = joint_state, mocap_state
		goal = self._goal_angle.copy()
		return base_state, goal

	def set_env_state(self, state):
		state, goal = state
		joint_state, mocap_state = state
		self.sim.set_state(joint_state)
		mocap_pos, mocap_quat = mocap_state
		self.data.set_mocap_pos('mocap', mocap_pos)
		self.data.set_mocap_quat('mocap', mocap_quat)
		self.sim.forward()
		self._goal_angle = goal

class SawyerDoorPushOpenEnv(SawyerDoorEnv):
	def __init__(self, min_angle=0, max_angle=.5, **kwargs):
		self.quick_init(locals())
		super().__init__(min_angle=min_angle, max_angle=max_angle, **kwargs)

	def mocap_set_action(self, action):
		pos_delta = action[None]
		self.reset_mocap2body_xpos()
		new_mocap_pos = self.data.mocap_pos + pos_delta
		new_mocap_pos[0, 0] = np.clip(
			new_mocap_pos[0, 0],
			-0.15,
			0.15,
		)
		new_mocap_pos[0, 1] = np.clip(
			new_mocap_pos[0, 1],
			0.5,
			2,
		)
		new_mocap_pos[0, 2] = np.clip(
			0.06,
			0,
			0.5,
		)
		self.data.set_mocap_pos('mocap', new_mocap_pos)
		self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))


class SawyerDoorPushOpenActionLimitedEnv(SawyerDoorPushOpenEnv):
	def __init__(self, max_x_pos=.1, max_y_pos=.7, **kwargs):
		self.quick_init(locals())
		self.max_x_pos = max_x_pos
		self.max_y_pos = max_y_pos
		self.min_y_pos = .5
		super().__init__(**kwargs)

	def mocap_set_action(self, action):
		pos_delta = action[None]
		self.reset_mocap2body_xpos()
		new_mocap_pos = self.data.mocap_pos + pos_delta
		new_mocap_pos[0, 0] = np.clip(
			new_mocap_pos[0, 0],
			-self.max_x_pos,
			self.max_x_pos,
		)
		new_mocap_pos[0, 1] = np.clip(
			new_mocap_pos[0, 1],
			self.min_y_pos,
			self.max_y_pos,
		)
		new_mocap_pos[0, 2] = np.clip(
			0.06,
			0,
			0.5,
		)
		self.data.set_mocap_pos('mocap', new_mocap_pos)
		self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))

	def set_to_goal(self, goal):
		ee_pos = np.random.uniform(np.array([-self.max_x_pos, self.min_y_pos, .06]),
								   np.array([self.max_x_pos, .6, .06]))
		self.set_goal_pos(ee_pos)
		self.set_goal_angle(goal)


class SawyerDoorPushOpenAndReachEnv(SawyerDoorPushOpenEnv):
	def __init__(self, frame_skip=30, min_angle=0, max_angle=.5, pos_action_scale=1 / 100,
				 action_reward_scale=0, goal=None, target_pos_scale=0, target_angle_scale=1):
		self.quick_init(locals())
		self.min_angle = min_angle
		self.max_angle = max_angle
		self.goal_space = Box(
			np.array([-.1, .5, .06, self.min_angle]),
			np.array([.1, .6, .06, self.max_angle]),
			# .6 is where the door is, only sample goals in the space between the door and the hand
		)
		if goal == None:
			goal = self.sample_goal_for_rollout()
		self.set_goal(goal)
		self._pos_action_scale = pos_action_scale
		self.target_pos_scale = target_pos_scale
		self.action_reward_scale = action_reward_scale
		self.target_angle_scale = target_angle_scale
		MujocoEnv.__init__(self, self.model_path, frame_skip=frame_skip)
		obs_space_angles = 1.5708  # this should not be changed, based on the xml

		self.action_space = Box(
			np.array([-1, -1, -1, -1]),
			np.array([1, 1, 1, 1]),
		)
		self.observation_space = Box(
			np.array([-1, -1, -1, -obs_space_angles]),
			np.array([1, 1, 1, obs_space_angles]),
		)

		self.reset()
		self.reset_mocap_welds()

	def step(self, a):
		a = np.clip(a, -1, 1)
		self.mocap_set_action(a[:3] * self._pos_action_scale)
		u = np.zeros((7))
		self.do_simulation(u, self.frame_skip)
		obs = self._get_obs()
		angle_dist, action_penalty, pos_dist = self._compute_reward(obs, self.get_goal(), a)
		reward = -1 * (
		angle_dist * self.target_angle_scale + action_penalty * self.action_reward_scale + pos_dist * self.target_pos_scale)
		done = False
		info = dict(
			angle_difference=angle_dist,
			distance=pos_dist,
			total_distance=angle_dist + pos_dist,
		)
		return obs, reward, done, info

	def _compute_reward(self, obs, goal, action):
		actual_angle = obs[-1]
		goal_angle = goal[-1]
		pos = obs[:3]
		goal_pos = goal[:3]
		return np.abs(actual_angle - goal_angle), np.linalg.norm(action), np.linalg.norm(pos - goal_pos)

	''' Multitask Functions '''

	@property
	def goal_dim(self):
		return 4

	def sample_goals(self, batch_size):
		return np.random.uniform(self.goal_space.low, self.goal_space.high, batch_size)

	def convert_obs_to_goals(self, obs):
		return obs

	def compute_her_reward_np(self, ob, action, next_ob, goal, env_info=None):
		angle_dist, action_penalty, pos_dist = self._compute_reward(next_ob, goal, action)
		reward = -1 * (
			angle_dist * self.target_angle_scale + action_penalty * self.action_reward_scale + pos_dist * self.target_pos_scale)
		return reward

	def set_goal_angle(self, angle):
		self._goal_angle = angle.copy()
		qpos = self.data.qpos.flat.copy()
		qvel = self.data.qvel.flat.copy()
		qpos[0] = angle.copy()
		qvel[0] = 0
		self.set_state(qpos, qvel)

	def set_goal_pos(self, xyz):
		for _ in range(10):
			self.data.set_mocap_pos('mocap', np.array(xyz))
			self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
			u = np.zeros(7)
			self.do_simulation(u, self.frame_skip)

	def set_goal(self, goal):
		MultitaskEnv.set_goal(self, goal)
		self._goal_angle = goal[-1]
		self._goal_pos = goal[:3]

	def set_to_goal(self, goal):
		self.set_goal_pos(goal[:3])
		self.set_goal_angle(goal[-1])

	def get_goal(self):
		return np.array([self._goal_pos[0], self._goal_pos[1], self._goal_pos[2], self._goal_angle])

	def log_diagnostics(self, paths, logger=logger, prefix=""):
		statistics = OrderedDict()
		for stat_name in [
			'angle_difference',
			'distance',
			'total_distance',
		]:
			stat = get_stat_in_paths(paths, 'env_infos', stat_name)
			statistics.update(create_stats_ordered_dict(
				'%s %s' % (prefix, stat_name),
				stat,
				always_show_all_stats=True,
			))
			statistics.update(create_stats_ordered_dict(
				'Final %s %s' % (prefix, stat_name),
				[s[-1] for s in stat],
				always_show_all_stats=True,
			))

		for key, value in statistics.items():
			logger.record_tabular(key, value)


class SawyerDoorPullOpenEnv(SawyerDoorEnv):
	def __init__(self, min_angle=-.5, max_angle=0, **kwargs):
		self.quick_init(locals())
		super().__init__(min_angle=min_angle, max_angle=max_angle, **kwargs)

	def mocap_set_action(self, action):
		pos_delta = action[None]
		self.reset_mocap2body_xpos()
		new_mocap_pos = self.data.mocap_pos + pos_delta
		new_mocap_pos[0, 0] = np.clip(
			new_mocap_pos[0, 0],
			-0.15,
			0.15,
		)
		new_mocap_pos[0, 1] = np.clip(
			new_mocap_pos[0, 1],
			-3,
			0.7,
		)
		new_mocap_pos[0, 2] = np.clip(
			0.06,
			0,
			0.5,
		)
		self.data.set_mocap_pos('mocap', new_mocap_pos)
		self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))


if __name__ == "__main__":
	import pygame
	from pygame.locals import QUIT, KEYDOWN

	# pygame.init()
	#
	# screen = pygame.display.set_mode((400, 300))

	char_to_action = {
		'w': np.array([0, 1, 0, 0]),
		'a': np.array([-1, 0, 0, 0]),
		's': np.array([0, -1, 0, 0]),
		'd': np.array([1, 0, 0, 0]),
		'q': np.array([1, -1, 0, 0]),
		'e': np.array([-1, -1, 0, 0]),
		'z': np.array([1, 1, 0, 0]),
		'c': np.array([-1, 1, 0, 0]),
		'x': 'toggle',
		'r': 'reset',
	}

	env = SawyerDoorPushOpenActionLimitedEnv(pos_action_scale=1 / 100)

	env = MultitaskToFlatEnv(env)

	policy = ZeroPolicy(env.action_space.low.size)
	es = OUStrategy(
		env.action_space,
		theta=1
	)
	es = EpsilonGreedy(
		action_space=env.action_space,
		prob_random_action=0.1,
	)
	policy = exploration_policy = PolicyWrappedWithExplorationStrategy(
		exploration_strategy=es,
		policy=policy,
	)

	env.reset()
	ACTION_FROM = 'hardcoded'
	# ACTION_FROM = 'pd'
	# ACTION_FROM = 'random'
	H = 100000
	# H = 300
	# H = 50
	goal = .25

	while True:
		lock_action = False
		obs = env.reset()
		last_reward_t = 0
		returns = 0
		action, _ = policy.get_action(None)
		for t in range(H):
			# done = False
			# if ACTION_FROM == 'controller':
			#     if not lock_action:
			#         action = np.array([0, 0, 0, 0])
			#     for event in pygame.event.get():
			#         event_happened = True
			#         if event.type == QUIT:
			#             sys.exit()
			#         if event.type == KEYDOWN:
			#             char = event.dict['key']
			#             new_action = char_to_action.get(chr(char), None)
			#             if new_action == 'toggle':
			#                 lock_action = not lock_action
			#             elif new_action == 'reset':
			#                 done = True
			#             elif new_action is not None:
			#                 action = new_action
			#             else:
			#                 action = np.array([0, 0, 0, 0])
			#             print("got char:", char)
			#             print("action", action)
			#             print("angles", env.data.qpos.copy())
			#             print("position", env.get_endeff_pos())
			# elif ACTION_FROM=='hardcoded':
			#     action=np.array([0, 1, 0, 0])
			# else:
			#     action = env.action_space.sample()
			# if np.abs(env.data.qpos[0]-.01) < .001:
			#     print(env.get_endeff_pos())
			#     break
			# obs, reward, _, info = env.step(action)
			goal = env.sample_goal_for_rollout()
			print(goal)
			env.set_to_goal(goal)
			env.render()
			print(t)
			# if done:
			#     break
			print("new episode")