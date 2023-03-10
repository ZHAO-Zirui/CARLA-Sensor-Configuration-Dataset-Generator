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

    def _data_callback(self, data):
        if not self._is_recording:
            return
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
        self.target_actor = None
        self.sensor_objs = list()
        self.sensor_infos = sensor_infos
        self.output_directory_path = os.path.join(runtime.io_output_directory, self.name)
        self.logger_header = f'Job [{self.name}]: '

    def _enter_sync_mode(self):
        settings = self.world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = runtime.carla_fixed_delta_time
        self.world.apply_settings(settings)
        logger.info(f'{self.logger_header}CARLA Simulator enter sync mode')

    def _exit_sync_mode(self):
        settings = self.world.get_settings()
        settings.synchronous_mode = False
        settings.fixed_delta_seconds = 0.0
        self.world.apply_settings(settings)
        logger.info(f'{self.logger_header}CARLA Simulator exit sync mode')

    def _data_callback(self, data, name):
        if not self.is_recording:
            return
        if isinstance(data, carla.Image):
            # save_path = os.path.join(self.output_directory_path, dir_name, f'{data.frame}.png')
            # data.save_to_disk(save_path)
            print(data.frame)
            print(name)

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

        # load new world 
        self.world = self.client.load_world(runtime.carla_map_name)

        # spawn vehicle
        vehicle_bp = self.world.get_blueprint_library().find(runtime.carla_vehicle_bp_name)
        vehicle_tf = carla.Transform(
            carla.Location(runtime.carla_vehicle_transform[0], 
                           runtime.carla_vehicle_transform[1],
                           runtime.carla_vehicle_transform[2]),
            carla.Rotation(runtime.carla_vehicle_transform[3], 
                           runtime.carla_vehicle_transform[4],
                           runtime.carla_vehicle_transform[5])
        )
        self.vehicle_actor = self.world.try_spawn_actor(vehicle_bp, vehicle_tf)
        logger.info(f'{self.logger_header}Vehicle [{runtime.carla_vehicle_bp_name}] spawnned at [{runtime.carla_vehicle_transform}]')
        
        # spawn target
        target_bp_name = random.choice(runtime.carla_target_bp_name_options)
        target_bp = self.world.get_blueprint_library().find(target_bp_name)
        target_init_r = random.uniform(runtime.carla_target_r_min, runtime.carla_target_r_max)
        target_tf = carla.Transform(
            carla.Location(target_init_r, 0, 0),
            carla.Rotation(0, 0, 0)
        )
        self.target_actor = self.world.try_spawn_actor(target_bp, target_tf)
        logger.info(f'{self.logger_header}Vehicle [{target_bp_name}] spawnned at [({target_init_r}, 0, 0, 0, 0, 0)]')

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

        # enter sync mode
        self._enter_sync_mode()
        time.sleep(runtime.carla_sync_wait_time)

        # main loop
        counter = 0
        max_counter = runtime.carla_sim_max_count
        while counter < max_counter:
            if counter == 0:
                for s in self.sensor_objs:
                    s.start_recording()
            counter += 1
            target_r = random.uniform(runtime.carla_target_r_min, runtime.carla_target_r_max)
            target_x = random.uniform(-1.0 * target_r, 1.0 * target_r)
            target_y = random.choice((-1.0, 1.0)) * math.pow((math.pow(target_r, 2) - math.pow(target_x, 2)), 0.5)
            target_yaw = random.uniform(0, 360)
            target_new_tf = carla.Transform(
                carla.Location(target_x, target_y, 0),
                carla.Rotation(0, target_yaw, 0))
            self.target_actor.set_transform(target_new_tf)
            logger.info(f'{self.logger_header}[{counter}/{max_counter}] Collecting sensor data. Target info: ' +
                        f'[x:{round(target_x, 2)}, y:{round(target_y, 2)}, r:{round(target_r, 2)}, yaw:{round(target_yaw, 2)}]')
            self.world.tick()
            time.sleep(runtime.carla_sim_step_time)

        for s in self.sensor_objs:
            s.stop_recording()

        # exit sync mode
        self._exit_sync_mode()

        # end func
        logger.success(f'{self.logger_header}Job completed.')
        return self
    
class JobYamlLoader:
    def __init__(self) -> None:
        self.logger_header = f'JobYamlLoader: '

    def load_demo(self) -> list:
        demo_sensor_0 = SensorInfo('sensor.camera.rgb', carla.Transform(carla.Location(x=0.8, z=1.7)))
        demo_sensor_1 = SensorInfo('sensor.lidar.ray_cast', carla.Transform(carla.Location(z=1.7)))
        demo_sensor_2 = SensorInfo('sensor.other.radar', carla.Transform(carla.Location(z=1.7)))
        job = Job('Demo', {demo_sensor_0, demo_sensor_1, demo_sensor_2})
        logger.warning(f'{self.logger_header}Job [Demo] loaded. Application is running in demo mode')
        return {job}
        
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

    # region Arguments & Runtime Setup
    # setup argparse
    parser = argparse.ArgumentParser(description=text.argparse_description, epilog=text.argparse_epilog, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--carla-ip-addr', type=str, help=text.argparse_carla_ip_addr, default=runtime.carla_ip_addr)
    parser.add_argument('--carla-port', type=int, help=text.argparse_carla_port, default=runtime.carla_port)
    parser.add_argument('-i', '--input', type=str, help=text.argparse_input, default=runtime.io_input_directory)
    parser.add_argument('-o', '--output', type=str, help=text.argparse_output, default=runtime.io_output_directory)
    parser.add_argument('--demo', help=text.argparse_demo, action="store_true")
    parser.add_argument('--r-min', type=float, help=text.argparse_r_min, default=runtime.carla_target_r_min)
    parser.add_argument('--r-max', type=float, help=text.argparse_r_max, default=runtime.carla_target_r_max)
    parser.add_argument('-c', '--count', type=int, help=text.argparse_count, default=runtime.carla_sim_max_count)
    parser.add_argument('-d', '--delta-t', type=float, help=text.argparse_help_none, default=runtime.carla_sim_step_time)
    parser.add_argument('--log', type=str, help=text.argparse_help_none, default=runtime.app_loguru_level)
    
    # setup runtimes
    args = parser.parse_args()
    runtime.app_root_path = os.path.abspath(os.path.dirname(__file__))
    runtime.app_loguru_level = args.log
    runtime.carla_ip_addr = args.carla_ip_addr
    runtime.carla_port = args.carla_port
    runtime.io_input_directory = os.path.join(runtime.app_root_path, os.path.normpath(args.input))
    runtime.io_output_directory = os.path.join(runtime.app_root_path, os.path.normpath(args.output))
    runtime.app_is_demo = args.demo
    runtime.carla_target_r_min = args.r_min
    runtime.carla_target_r_max = args.r_max
    runtime.carla_sim_max_count = args.count
    runtime.carla_sim_step_time = args.delta_t
    # log args decode and runtime setup complete
    logger.success('Runtime loads complete.')
    # endregion

    # region Jobs
    jobs = list()
    job_loader = JobYamlLoader()
    if runtime.app_is_demo:
        jobs.extend(job_loader.load_demo())
    else:
        jobs.extend(job_loader.load_all(runtime.io_input_directory))
    
    for job in jobs:
        if not isinstance(job, Job):
            continue
        job.setup()
        job.exec()
    # endregion
    