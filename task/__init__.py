from gym.envs.registration import register

# register custom tasks
register(
    id='CartPoleTask-v0',
    entry_point='task.task:CartPoleTask',
    max_episode_steps=200,
    reward_threshold=195.0,
)