import sys
import os
import argparse
import text
import runtime
import carla
import random
import time
import math
import shutil
import csv
import yaml
from asq import query
from loguru import logger

# region Classes
class ScenarioInfo:
    def __init__(self, name, path, t_start, t_end, ego_actor_id) -> None:
        self.name = name
        self.record_path = os.path.join(runtime.app_root_path, os.path.abspath(path))
        self.time_start = t_start
        self.time_end = t_end
        self.ego_vehicle_actor_id = ego_actor_id

class ScenarioInfoYamlLoader:
    def __init__(self) -> None:
        self.logger_header = f'ScenarioInfoYamlLoader: '
    
    def load_all(self) -> list:
        scenario_list = list()
        yaml_file_abspath = os.path.abspath(os.path.join(runtime.app_root_path, os.path.normpath(runtime.scenario_config_filepath)))
        logger.info(f'{self.logger_header}Starts loading YAML scenario info file from: [{yaml_file_abspath}]')
        if not os.path.exists(yaml_file_abspath):
            logger.error(f'{self.logger_header}Input Error: ScenarioInfo file [{yaml_file_abspath}] not exists.')
            exit(1)
        # exec load
        with open(yaml_file_abspath, 'r', encoding='utf8') as f:
            yaml_data = yaml.load(f, Loader=yaml.FullLoader)
        # decode yaml file
        for d in yaml_data:
            info = ScenarioInfo(d['name'], d['record_file'], d['time']['start'], d['time']['end'], d['ego_vehicle_actor_id'])
            logger.debug(f'{self.logger_header}{info.name}/record_path: [{info.record_path}]')
            logger.debug(f'{self.logger_header}{info.name}/time_start: [{info.time_start}]')
            logger.debug(f'{self.logger_header}{info.name}/time_end: [{info.time_end}]')
            logger.debug(f'{self.logger_header}{info.name}/ego_vehicle_actor_id: [{info.ego_vehicle_actor_id}]')
            scenario_list.append(info)
            logger.success(f'{self.logger_header}Successfully loading scenario: [{info.name}]')
        return scenario_list

class SensorInfo:
    def __init__(self, blueprint_name, transform=carla.Transform) -> None:
        self.blueprint_name = blueprint_name
        self.transform = transform
        self.attribute = dict()

class Sensor:
    def __init__(self, info: SensorInfo, seq_id: int, job_log_header: str, world: carla.World, target: carla.Actor, output_dir) -> None:
        self.sensor_info = info
        self.seq_id = seq_id
        self.logger_header = job_log_header
        self._is_recording = False
        self.sensor_actor = None
        self.world = world
        self.vehicle_actor = target
        self.output_directory_path = output_dir
        self.record_counter_target = 0
        self.record_counter_current = 0

    def record_this(self):
        self.record_counter_target += 1
        logger.debug(f'{self.logger_header} received <record this> cmd, target: [{self.record_counter_target}]')


    def _data_callback(self, data):
        if not self._is_recording:
            return
        if self.record_counter_current >= self.record_counter_target:
            return
        self.record_counter_current += 1
        save_path = os.path.join(self.output_directory_path, str(self.seq_id))
        if isinstance(data, carla.Image):
            save_fullname = os.path.join(save_path, f'{data.frame}.png' )
            logger.debug(f'{self.logger_header}Sensor [{self.seq_id}] is saving image data to: [{save_fullname}]')
            data.save_to_disk(save_fullname)
        if isinstance(data, carla.LidarMeasurement):
            save_fullname = os.path.join(save_path, f'{data.frame}.ply' )
            logger.debug(f'{self.logger_header}Sensor [{self.seq_id}] is saving pointcloud(ply) data to: [{save_fullname}]')
            data.save_to_disk(save_fullname)
        if isinstance(data, carla.RadarMeasurement):
            save_fullname = os.path.join(save_path, f'{data.frame}.csv' )
            csv_headers = ['velocity', 'azimuth', 'altitude', 'depth']
            csv_rows = list()
            for d in data:
                csv_rows.append((d.velocity, d.azimuth, d.altitude, d.depth))
            if not os.path.exists(save_path):
                os.makedirs(save_path)
            with open(save_fullname, 'w', encoding='utf8', newline='') as f:
                w = csv.writer(f)
                w.writerow(csv_headers)
                w.writerows(csv_rows)
            logger.debug(f'{self.logger_header}Sensor [{self.seq_id}] is saving radar(csv) data to: [{save_fullname}]')
    
    def spawn(self):
         # get blueprint
        sensor_bp = self.world.get_blueprint_library().find(self.sensor_info.blueprint_name)
        logger.info(f'{self.logger_header}Sensor [{self.seq_id}] blueprint set to: [{sensor_bp.id}]')
        # get transform
        sensor_tf = self.sensor_info.transform
        logger.info(f'{self.logger_header}Sensor [{self.seq_id}] transform set to: [({sensor_tf.location.x}, {sensor_tf.location.y}, {sensor_tf.location.z}, '+
                    f'{sensor_tf.rotation.pitch}, {sensor_tf.rotation.yaw}, {sensor_tf.rotation.roll})]')
        # get attributes
        for (attr_key, attr_value) in self.sensor_info.attribute.items():
            try:
                sensor_bp.set_attribute(str(attr_key), str(attr_value))
                logger.info(f'{self.logger_header}Sensor [{self.seq_id}] set attribute as key:[{attr_key}], value:[{str(attr_value)}]')
            except IndexError:
                logger.error(f'{self.logger_header}Sensor [{self.seq_id}] set attribute failure with no key found:[{attr_key}]')
        # addition static attributes
        if sensor_bp.id == 'sensor.lidar.ray_cast':
            sensor_bp.set_attribute('points_per_second', str(1280000))
            sensor_bp.set_attribute('rotation_frequency', str(100))
        # spawn actor
        self.sensor_actor = self.world.spawn_actor(sensor_bp, sensor_tf, attach_to=self.vehicle_actor)
        self.sensor_actor.listen(lambda data: self._data_callback(data))

    def start_recording(self):
        logger.info(f'{self.logger_header}Sensor [{self.seq_id}] starts recording')
        self._is_recording = True

    def stop_recording(self):
        logger.info(f'{self.logger_header}Sensor [{self.seq_id}] stopped recording')
        self._is_recording = False

class Job:
    def __init__(self, job_name: str, sensor_infos: list) -> None:
        self.name = job_name
        self.client = None
        self.world = None
        self.vehicle_actor = None
        self.sensor_objs = list()
        self.sensor_infos = sensor_infos
        self._scenario_info = None

    @property
    def output_directory_path(self):
        return os.path.join(runtime.io_output_directory, self.name, self.scenario_info.name)
    
    @property
    def scenario_info(self) -> ScenarioInfo:
        return self._scenario_info
    
    @property
    def logger_header(self):
        if self._scenario_info:
            return f'Job [{self.name} / {self._scenario_info.name}]: '
        else:
            return f'Job [{self.name} / ?]: '

    def bind_scenario_info(self, info:ScenarioInfo):
        self._scenario_info =  info
        logger.info(f'{self.logger_header}Bind scenario info: [{self.scenario_info.name}]')

    def setup(self):
        logger.info(f'{self.logger_header}Begin setup')
        # create folder
        logger.info(f'{self.logger_header}Output directory set to: [{self.output_directory_path}]')
        if os.path.exists(self.output_directory_path) and os.path.isdir(self.output_directory_path):
            logger.warning(f'{self.logger_header}Output directory [{self.output_directory_path}] already exist, old ones will be deleted')
            shutil.rmtree(self.output_directory_path)
        os.makedirs(self.output_directory_path)
        logger.info(f'{self.logger_header}Output directory [{self.output_directory_path}] cureated complete')

        # connect to carla
        self.client = carla.Client(runtime.carla_ip_addr, runtime.carla_port)

        # decode scenario file and load new world
        world_name = self.client.show_recorder_file_info(self.scenario_info.record_path, False).splitlines()[1].replace('Map: ', '')
        logger.info(f'{self.logger_header}Load map[{world_name}] by sceanrio: [{self.scenario_info.name}]')
        self.world = self.client.load_world(world_name)

        # spawn scenario actors
        self.client.replay_file(self.scenario_info.record_path, 0.0, 0.3, self.scenario_info.ego_vehicle_actor_id, False)
        self.vehicle_actor = self.world.get_actor(self.scenario_info.ego_vehicle_actor_id)

        # spawn sensors
        sensor_counter = 0
        for sensor_info in self.sensor_infos:
            logger.info(f'{self.logger_header}Decoding sensor [{sensor_counter}]')
            sensor_obj = Sensor(sensor_info, sensor_counter, self.logger_header, self.world, self.vehicle_actor, self.output_directory_path)
            sensor_obj.spawn()
            self.sensor_objs.append(sensor_obj)
            sensor_counter += 1

        # wait until ready
        logger.info(f'{self.logger_header}Wait [{runtime.carla_setup_wait_time}] second(s) for setup simulation environment')
        time.sleep(runtime.carla_setup_wait_time)
        logger.success(f'{self.logger_header}Simualtion environment setup complete')

        # end func
        return self

    def exec(self):
        logger.info(f'{self.logger_header}Start execute')

        # calculate replay time
        replay_times = list()
        total_t = self.scenario_info.time_end - self.scenario_info.time_start
        delta_t = total_t / (runtime.carla_sim_max_count + 1)
        current_t = self.scenario_info.time_start
        for i in range(runtime.carla_sim_max_count):
            current_t = current_t + delta_t + random.uniform(-1.0 * runtime.carla_sim_time_random, runtime.carla_sim_time_random)
            replay_times.append(current_t)
        logger.info(f'{self.logger_header}Replay sequence (length:{len(replay_times)}) set to: [{replay_times}]')

        # main loop
        counter = 0
        max_counter = runtime.carla_sim_max_count
        while counter < max_counter:
            if counter == 0:
                for s in self.sensor_objs:
                    s.start_recording()
            counter += 1

            # update scenario
            self.client.replay_file(self.scenario_info.record_path, replay_times[counter-1], 0.1, self.scenario_info.ego_vehicle_actor_id, False)
            time.sleep(runtime.carla_sim_step_wait_scenario_time)

            # collect sensor data
            logger.info(f'{self.logger_header}[{counter}/{max_counter}] Collecting sensor data.')
            for s in self.sensor_objs:
                s.record_this()

            # self.world.tick()
            time.sleep(runtime.carla_sim_step_wait_record_time)

        # stop recording
        for s in self.sensor_objs:
            s.stop_recording()

        # end func
        logger.success(f'{self.logger_header}Job completed.')
        return self
    
    def clean(self):
        time.sleep(runtime.carla_setup_wait_time)
        sensor_actors = list()
        for s in self.sensor_objs:
            sensor_actors.append(s.sensor_actor)
            s.sensor_actor.stop()
        self.client.apply_batch([carla.command.DestroyActor(x) for x in sensor_actors])
        self.client = None
        self.world = None
        self.vehicle_actor = None
        self.sensor_objs = list()
        time.sleep(runtime.carla_setup_wait_time)

class JobYamlLoader:
    def __init__(self) -> None:
        self.logger_header = f'JobYamlLoader: '
        
    def load_all(self, load_path):
        logger.info(f'{self.logger_header}Starts loading YAML config files from: [{load_path}]')
        job_list = list()
        if not os.path.exists(load_path):
            logger.error(f'{self.logger_header}Input Error: Directory [{load_path}] not exists.')
            return job_list
        yaml_files = query(os.listdir(load_path)).where(lambda i: i.endswith('.yaml')).to_list()
        if not any(yaml_files):
            logger.error(f'{self.logger_header}Input Error: No available YAML config files.')
            return job_list
        for yaml_file in yaml_files:
            yaml_file_abspath = os.path.join(load_path, yaml_file)
            logger.info(f'{self.logger_header}Found yaml file: [{yaml_file_abspath}]')
            with open(yaml_file_abspath, 'r', encoding='utf8') as f:
                yaml_data = yaml.load(f, Loader=yaml.FullLoader)
            # create jobs
            job_name = yaml_file.replace('.yaml', '')
            logger.info(f'{self.logger_header}Creating new job: [{job_name}]')
            try:
                sensor_info_list = list()
                for yaml_sensor_info in yaml_data:
                    blueprint_name = yaml_sensor_info['blueprint_name']
                    yaml_transform = yaml_sensor_info['transform']
                    transform = carla.Transform(
                        carla.Location(yaml_transform['x'], yaml_transform['y'], yaml_transform['z']),
                        carla.Rotation(yaml_transform['pitch'], yaml_transform['yaw'], yaml_transform['roll'])
                    )
                    sensor_info = SensorInfo(blueprint_name, transform)
                    if yaml_sensor_info['attribute']:
                        sensor_info.attribute = yaml_sensor_info['attribute']
                    sensor_info_list.append(sensor_info)
                    logger.info(f'{self.logger_header}Load sensor: [{blueprint_name}]')
            except IndexError:
                logger.error(f'{self.logger_header}Decode Error: Broken YAML file [{yaml_file_abspath}]')
                continue
            except KeyError:
                logger.error(f'{self.logger_header}Decode Error: Broken YAML file [{yaml_file_abspath}]')
                continue
            job = Job(job_name, sensor_info_list)
            job_list.append(job)
            logger.success(f'{self.logger_header}Successfully loading job: [{job_name}]')
        return job_list
# endregion


# MAIN ENTRY
if __name__=='__main__':
    # region Arguments & Runtime Setup
    # setup argparse
    parser = argparse.ArgumentParser(description=text.argparse_description, epilog=text.argparse_epilog, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--carla-ip-addr', type=str, help=text.argparse_carla_ip_addr, default=runtime.carla_ip_addr)
    parser.add_argument('--carla-port', type=int, help=text.argparse_carla_port, default=runtime.carla_port)
    parser.add_argument('-i', '--input', type=str, help=text.argparse_input, default=runtime.io_input_directory)
    parser.add_argument('-o', '--output', type=str, help=text.argparse_output, default=runtime.io_output_directory)
    parser.add_argument('-c', '--count', type=int, help=text.argparse_count, default=runtime.carla_sim_max_count)
    parser.add_argument('-s', '--wait-scenario', type=float, help=text.argparse_wait_scenario, default=runtime.carla_sim_step_wait_scenario_time)
    parser.add_argument('-r', '--wait-record', type=float, help=text.argparse_wait_record, default=runtime.carla_sim_step_wait_record_time)
    parser.add_argument('--random', type=float, help=text.argparse_random, default=runtime.carla_sim_time_random)
    parser.add_argument('--log', type=str, help=text.argparse_help_none, default=runtime.app_loguru_level)
    
    # setup runtimes
    args = parser.parse_args()
    runtime.app_root_path = os.path.abspath(os.path.dirname(__file__))
    runtime.app_loguru_level = args.log
    runtime.carla_ip_addr = args.carla_ip_addr
    runtime.carla_port = args.carla_port
    runtime.io_input_directory = os.path.join(runtime.app_root_path, os.path.normpath(args.input))
    runtime.io_output_directory = os.path.join(runtime.app_root_path, os.path.normpath(args.output))
    runtime.carla_sim_max_count = args.count
    runtime.carla_sim_step_wait_scenario_time = args.wait_scenario
    runtime.carla_sim_step_wait_record_time = args.wait_record
    runtime.carla_sim_time_random = args.random
    # log args decode and runtime setup complete
    logger.success('Runtime loads complete.')
    # endregion

    # region Application Setup
    # setup logger
    logger.remove()
    logger.add(sys.stdout, 
        colorize=True, 
        format=runtime.app_loguru_format,
        level=runtime.app_loguru_level)
    
    # log app start to confirm entry
    logger.success('Application start.')
    # endregion

    # region MAIN
    job_loader = JobYamlLoader()
    loaded_jobs = job_loader.load_all(runtime.io_input_directory)

    scenario_info_loader = ScenarioInfoYamlLoader()
    loaded_scenario_infos = scenario_info_loader.load_all()
    
    # start exec jobs
    logger.success('='*20 + 'BEGIN JOB EXEC' + '='*20)
    for job in loaded_jobs:
        for scenario_info in loaded_scenario_infos:
            job.bind_scenario_info(scenario_info)
            if not isinstance(job, Job):
                continue
            job.setup()
            job.exec()
            job.clean()

    logger.success('='*20 + 'FINISH JOB EXEC' + '='*20)
    logger.success('DONE.')
    # endregion
    