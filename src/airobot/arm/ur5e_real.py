"""
A UR5e robot with a robotiq 2f140 gripper
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import sys
import threading
import time

import PyKDL as kdl
import moveit_commander
import numpy as np
import rospy
import tf
from kdl_parser_py.urdf import treeFromParam
from moveit_commander import MoveGroupCommander
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from trac_ik_python import trac_ik
from trajectory_msgs.msg import JointTrajectory
from trajectory_msgs.msg import JointTrajectoryPoint

import airobot.utils.common as arutil
from airobot.arm.arm import ARM
from airobot.utils.arm_util import wait_to_reach_ee_goal
from airobot.utils.arm_util import wait_to_reach_jnt_goal
from airobot.utils.common import joints_to_kdl
from airobot.utils.common import kdl_array_to_numpy
from airobot.utils.common import kdl_frame_to_numpy
from airobot.utils.common import print_red
from airobot.utils.moveit_util import MoveitScene
from airobot.utils.ros_util import get_tf_transform


class UR5eReal(ARM):
    def __init__(self, cfgs,
                 moveit_planner='RRTstarkConfigDefault',
                 eetool_cfg=None):
        """

        Args:
            cfgs (YACS CfgNode): configurations for the arm
            moveit_planner (str): motion planning algorithm
            eetool_cfg (dict): arguments to pass in the constructor
                of the end effector tool class
        """
        super(UR5eReal, self).__init__(cfgs=cfgs, eetool_cfg=eetool_cfg)

        self.moveit_planner = moveit_planner
        self.gazebo_sim = rospy.get_param('sim')
        self._init_consts()

        if not self.gazebo_sim:
            self.robot_ip = rospy.get_param('robot_ip')
            self.set_comm_mode()
            self._setup_pub_sub()
            self._set_tool_offset()
        else:
            self.set_comm_mode(use_urscript=False)

        time.sleep(1.0)  # sleep to give subscribers time to connect

    def set_comm_mode(self, use_urscript=False):
        """
        Method to set whether to use ros or urscript to control the real robot

        Arguments:
            use_urscript (bool): True we should use urscript
                False if we should use ros and moveit
        """
        self.use_urscript = use_urscript

    def go_home(self):
        """
        Move the robot to a pre-defined home pose
        """
        self.set_jpos(self._home_position, wait=True)

    def set_jpos(self, position, joint_name=None, wait=True, *args, **kwargs):
        """
        Method to send a joint position command to the robot (units in rad)

        Args:
            position (float or list or flattened np.ndarray):
                desired joint position(s)
                (shape: [6,] if list, otherwise a single value)
            joint_name (str): If not provided, position should be a list and
                all actuated joints will be moved to specified positions. If
                provided, only specified joint will move. Defaults to None
            wait (bool): whether position command should be blocking or non
                blocking. Defaults to True

        Return:
            success (bool): True if command was completed successfully, returns
                False if wait flag is set to False.
        """
        position = copy.deepcopy(position)
        success = False

        if joint_name is None:
            if len(position) != 6:
                raise ValueError('position should contain 6 elements if'
                                 'joint_name is not provided')
            tgt_pos = position
        else:
            if not isinstance(position, float):
                raise TypeError('position should be individual float value'
                                'if joint_name is provided')
            if joint_name not in self.arm_jnt_names_set:
                raise TypeError('Joint name [%s] is not in the arm'
                                ' joint list!' % joint_name)
            else:
                tgt_pos = self.get_jpos()
                arm_jnt_idx = self.arm_jnt_names.index(joint_name)
                tgt_pos[arm_jnt_idx] = position
        if self.use_urscript:
            prog = 'movej([%f, %f, %f, %f, %f, %f])' % (tgt_pos[0],
                                                        tgt_pos[1],
                                                        tgt_pos[2],
                                                        tgt_pos[3],
                                                        tgt_pos[4],
                                                        tgt_pos[5])
            self._send_urscript(prog)

            if wait:
                success = wait_to_reach_jnt_goal(
                    position,
                    get_func=self.get_jpos,
                    joint_name=joint_name,
                    get_func_derv=self.get_jvel,
                    timeout=self.cfgs.ARM.TIMEOUT_LIMIT,
                    max_error=self.cfgs.ARM.MAX_JOINT_ERROR
                )
        else:
            self.moveit_group.set_joint_value_target(tgt_pos)
            success = self.moveit_group.go(tgt_pos, wait=wait)

        return success

    def set_jvel(self, velocity, acc=0.1, joint_name=None, wait=False,
                 *args, **kwargs):
        """
        Set joint velocity command to the robot (units in rad/s)

        Args:
            velocity (float or list or flattened np.ndarray): list of target
                 joint velocity value(s)
                (shape: [6,] if list, otherwise a single value)
            acc (float): Value with which to accelerate when robot starts
                moving. Only used if self.use_urscript=True. Defaults to 0.1.
            joint_name (str, optional): If not provided, velocity should be
                list and all joints will be turned on at specified velocity.
                Defaults to None.
            wait (bool, optional): If True, block until robot reaches
                desired joint velocity value(s). Defaults to False.
        """
        velocity = copy.deepcopy(velocity)
        success = False

        if self.gazebo_sim:
            raise NotImplementedError('cannot set_jvel in Gazebo')

        if joint_name is None:
            if len(velocity) != 6:
                raise ValueError('Velocity should contain 6 elements'
                                 'if the joint name is not provided')
            tgt_vel = velocity
        else:
            if not isinstance(velocity, float):
                raise TypeError('velocity should be individual float value'
                                'if joint_name is provided')
            if joint_name not in self.arm_jnt_names_set:
                raise TypeError('Joint name [%s] is not in the arm'
                                ' joint list!' % joint_name)
            else:
                tgt_vel = [0.0] * len(self.arm_jnt_names)
                arm_jnt_idx = self.arm_jnt_names.index(joint_name)
                tgt_vel[arm_jnt_idx] = velocity
        if self.use_urscript:
            prog = 'speedj([%f, %f, %f, %f, %f, %f], a=%f)' % (tgt_vel[0],
                                                               tgt_vel[1],
                                                               tgt_vel[2],
                                                               tgt_vel[3],
                                                               tgt_vel[4],
                                                               tgt_vel[5],
                                                               acc)
            self._send_urscript(prog)
        else:
            # self._pub_joint_vel(tgt_vel)
            raise NotImplementedError('set_jvel only works '
                                      'with use_urscript=True')

        if wait:
            success = wait_to_reach_jnt_goal(
                velocity,
                get_func=self.get_jvel,
                joint_name=joint_name,
                timeout=self.cfgs.ARM.TIMEOUT_LIMIT,
                max_error=self.cfgs.ARM.MAX_JOINT_VEL_ERROR
            )

        return success

    def set_ee_pose(self, pos=None, ori=None, acc=0.2, vel=0.3, wait=True,
                    ik_first=False, *args, **kwargs):
        """
        Set cartesian space pose of end effector

        Args:
            pos (list or np.ndarray): Desired x, y, z positions in the robot's
                base frame to move to (shape: [3,])
            ori (list or np.ndarray, optional): It can be euler angles
                ([roll, pitch, yaw], shape: [4,]),
                or quaternion ([qx, qy, qz, qw], shape: [4,]),
                or rotation matrix (shape: [3, 3]). If it's None,
                the solver will use the current end effector
                orientation as the target orientation
            acc (float, optional): Acceleration of end effector during
                beginning of movement. This arg takes effect only when
                self.use_urscript is True and ik_first is False.
                Defaults to 0.3.
            vel (float, optional): Velocity of end effector during movement.
                This parameter takes effect only when self.use_urscript is
                 True and ik_first is False. Defaults to 0.2.
            ik_first (bool, optional): Whether to use the solution computed
                by IK, or to use UR built in movel function which moves
                linearly in tool space (movel may sometimes fail due to
                sinularities). This parameter takes effect only when
                self.use_urscript is True. Defaults to False.

        Returns:
            bool: Returns True is robot successfully moves to goal pose
        """
        if ori is None and pos is None:
            return True
        success = False
        if ori is None:
            pose = self.get_ee_pose()  # last index is euler angles
            quat = pose[1]
        else:
            ori = np.array(ori)
            if ori.size == 4:
                quat = ori
            elif ori.size == 3:
                quat = arutil.euler2quat(ori)
            elif ori.shape == (3, 3):
                quat = arutil.rot2quat(ori)
            else:
                raise ValueError('Orientaion should be quaternion,'
                                 ' euler angles, or rotation matrix')
        if pos is None:
            pose = self.get_ee_pose()
            pos = pose[0]

        if self.use_urscript:
            if ik_first:
                jnt_pos = self.compute_ik(pos, quat)
                # use movej instead of movel
                success = self.set_jpos(jnt_pos, wait=wait)
            else:
                rotvec = arutil.quat2rotvec(quat)
                ee_pos = [pos[0], pos[1], pos[2],
                          rotvec[0], rotvec[1], rotvec[2]]
                # movel moves linear in tool space, may go through singularity
                # orientation in movel is specified as a rotation vector!
                # not euler angles!
                prog = 'movel(p[%f, %f, %f, %f, %f, %f], a=%f, v=%f, r=%f)' % (
                    ee_pos[0],
                    ee_pos[1],
                    ee_pos[2],
                    ee_pos[3],
                    ee_pos[4],
                    ee_pos[5],
                    acc,
                    vel,
                    0.0)
                self._send_urscript(prog)
                if wait:
                    args_dict = {
                        'get_func': self.get_ee_pose,
                        'get_func_derv': self.get_ee_vel,
                        'timeout': self.cfgs.ARM.TIMEOUT_LIMIT,
                        'pos_tol': self.cfgs.ARM.MAX_EE_POS_ERROR,
                        'ori_tol': self.cfgs.ARM.MAX_EE_ORI_ERROR
                    }
                    success = wait_to_reach_ee_goal(pos, quat,
                                                    **args_dict)
        else:
            pose = self.moveit_group.get_current_pose()
            pose.pose.position.x = pos[0]
            pose.pose.position.y = pos[1]
            pose.pose.position.z = pos[2]
            pose.pose.orientation.x = quat[0]
            pose.pose.orientation.y = quat[1]
            pose.pose.orientation.z = quat[2]
            pose.pose.orientation.w = quat[3]
            self.moveit_group.set_pose_target(pose)
            success = self.moveit_group.go(wait=True)
        return success

    def move_ee_xyz(self, delta_xyz, eef_step=0.005, wait=True,
                    *args, **kwargs):
        """
        Move end effector in straight line while maintaining orientation

        Args:
            delta_xyz (list or np.ndarray): Goal change in x, y, z position of
                end effector
            eef_step (float, optional): Discretization step in cartesian space
                for computing waypoints along the path. Defaults to 0.005 (m).
            wait (bool, optional): True if robot should not do anything else
                until this goal is reached, or the robot times out.
                Defaults to True.

        Returns:
            bool: True if robot successfully reached the goal pose
        """
        ee_pos, ee_quat, ee_rot_mat, ee_euler = self.get_ee_pose()

        if self.use_urscript:
            ee_pos[0] += delta_xyz[0]
            ee_pos[1] += delta_xyz[1]
            ee_pos[2] += delta_xyz[2]
            success = self.set_ee_pose(ee_pos, ee_euler, wait=wait,
                                       ik_first=False)
        else:
            cur_pos = np.array(ee_pos)
            cur_quat = ee_quat
            delta_xyz = np.array(delta_xyz)
            end_pos = cur_pos + delta_xyz
            moveit_waypoints = []
            wpose = self.moveit_group.get_current_pose().pose
            wpose.position.x = cur_pos[0]
            wpose.position.y = cur_pos[1]
            wpose.position.z = cur_pos[2]
            wpose.orientation.x = cur_quat[0]
            wpose.orientation.y = cur_quat[1]
            wpose.orientation.z = cur_quat[2]
            wpose.orientation.w = cur_quat[3]
            moveit_waypoints.append(copy.deepcopy(wpose))

            wpose.position.x = end_pos[0]
            wpose.position.y = end_pos[1]
            wpose.position.z = end_pos[2]
            wpose.orientation.x = cur_quat[0]
            wpose.orientation.y = cur_quat[1]
            wpose.orientation.z = cur_quat[2]
            wpose.orientation.w = cur_quat[3]
            moveit_waypoints.append(copy.deepcopy(wpose))

            (plan, fraction) = self.moveit_group.compute_cartesian_path(
                moveit_waypoints,  # waypoints to follow
                eef_step,  # eef_step
                0.0)  # jump_threshold
            success = self.moveit_group.execute(plan, wait=wait)
        return success

    def get_jpos(self, joint_name=None):
        """
        Gets the current joint position of the robot. Gets the value
        from the internally updated dictionary that subscribes to the ROS
        topic /joint_states

        Args:
            joint_name (str, optional): If it's None,
                it will return joint positions
                of all the actuated joints. Otherwise, it will
                return the joint position of the specified joint

        Returns:
            float: joint position given joint_name
            or
            list: joint positions if joint_name is None (shape: [6,])

        """
        self._j_state_lock.acquire()
        if joint_name is not None:
            if joint_name not in self.arm_jnt_names:
                raise TypeError('Joint name [%s] not recognized!' % joint_name)
            jpos = self._j_pos[joint_name]
        else:
            jpos = []
            for joint in self.arm_jnt_names:
                jpos.append(self._j_pos[joint])
        self._j_state_lock.release()
        return jpos

    def get_jvel(self, joint_name=None):
        """
        Gets the current joint angular velocities of the robot. Gets the value
        from the internally updated dictionary that subscribes to the ROS
        topic /joint_states

        Args:
            joint_name (str, optional): If it's None,
                it will return joint velocities
                of all the actuated joints. Otherwise, it will
                return the joint position of the specified joint

        Returns:
            float: joint velocity given joint_name
            or
            list: joint velocities if joint_name is None (shape: [6,])
        """
        self._j_state_lock.acquire()
        if joint_name is not None:
            if joint_name not in self.arm_jnt_names:
                raise TypeError('Joint name [%s] not recognized!' % joint_name)
            jvel = self._j_vel[joint_name]
        else:
            jvel = []
            for joint in self.arm_jnt_names:
                jvel.append(self._j_vel[joint])
        self._j_state_lock.release()
        return jvel

    def get_ee_pose(self):
        """
        Get current cartesian pose of the EE, in the robot's base frame,
        using ROS subscriber to the tf tree topic

        Returns:
            np.ndarray: x, y, z position of the EE (shape: [3])
            np.ndarray: quaternion representation ([x, y, z, w]) of the EE
                orientation (shape: [4])
            np.ndarray: rotation matrix representation of the EE orientation
                (shape: [3, 3])
            np.ndarray: euler angle representation of the EE orientation (roll,
                pitch, yaw with static reference frame) (shape: [3])
        """
        pos, quat = get_tf_transform(self.tf_listener,
                                     self.cfgs.ARM.ROBOT_BASE_FRAME,
                                     self.cfgs.ARM.ROBOT_EE_FRAME)
        rot_mat = arutil.quat2rot(quat)
        euler_ori = arutil.quat2euler(quat)
        return np.array(pos), np.array(quat), rot_mat, euler_ori

    def get_ee_vel(self):
        """
        Return the end effector's velocity

        Returns:
            np.ndarray: translational velocity (vx, vy, vz) (shape: [3,])
            np.ndarray: rotational velocity (wx, wy, wz) (shape: [3,])
        """
        jpos = self.get_jpos()
        jvel = self.get_jvel()
        ee_vel = self.compute_fk_velocity(jpos, jvel,
                                          self.cfgs.ARM.ROBOT_EE_FRAME)
        return ee_vel[:3], ee_vel[3:]

    def get_jacobian(self, joint_angles):
        """
        Return the geometric jacobian on the given joint angles.
        Refer to P112 in "Robotics: Modeling, Planning, and Control"

        Args:
            joint_angles (list or flattened np.ndarray): joint angles

        Returns:
            np.ndarray: jacobian (shape: [6, 6])
        """
        q = kdl.JntArray(self.urdf_chain.getNrOfJoints())
        for i in range(q.rows()):
            q[i] = joint_angles[i]
        jac = kdl.Jacobian(self.urdf_chain.getNrOfJoints())
        fg = self.jac_solver.JntToJac(q, jac)
        assert fg == 0, 'KDL JntToJac error!'
        jac_np = kdl_array_to_numpy(jac)
        return jac_np

    def compute_fk_position(self, jpos, tgt_frame):
        """
        Given joint angles, compute the pose of desired_frame with respect
        to the base frame (self.cfgs.ARM.ROBOT_BASE_FRAME). The desired frame
        must be in self.arm_link_names

        Args:
            jpos (list or flattened np.ndarray): joint angles
            tgt_frame (str): target link frame

        Returns:
            np.ndarray: translational vector (shape: [3,])
            np.ndarray: rotational matrix (shape: [3, 3])
        """
        if isinstance(jpos, list):
            jpos = np.array(jpos)
        jpos = jpos.flatten()
        if jpos.size != self.arm_dof:
            raise ValueError('Length of the joint angles '
                             'does not match the robot DOF')
        assert jpos.size == self.arm_dof
        kdl_jnt_angles = joints_to_kdl(jpos)

        kdl_end_frame = kdl.Frame()
        idx = self.arm_link_names.index(tgt_frame) + 1
        fg = self.fk_solver_pos.JntToCart(kdl_jnt_angles,
                                          kdl_end_frame,
                                          idx)
        if fg == 0:
            raise ValueError('KDL Pos JntToCart error!')
        pose = kdl_frame_to_numpy(kdl_end_frame)
        pos = pose[:3, 3].flatten()
        rot = pose[:3, :3]
        return pos, rot

    def compute_fk_velocity(self, jpos, jvel, tgt_frame):
        """
        Given joint_positions and joint velocities,
        compute the velocities of tgt_frame with respect
        to the base frame

        Args:
            jpos (list or flattened np.ndarray): joint positions
            jvel (list or flattened np.ndarray): joint velocities
            tgt_frame (str): target link frame

        Returns:
            np.ndarray: translational and rotational
                 velocities (vx, vy, vz, wx, wy, wz)
                 (shape: [6,])
        """
        if isinstance(jpos, list):
            jpos = np.array(jpos)
        if isinstance(jvel, list):
            jvel = np.array(jvel)
        kdl_end_frame = kdl.FrameVel()
        kdl_jnt_angles = joints_to_kdl(jpos)
        kdl_jnt_vels = joints_to_kdl(jvel)
        kdl_jnt_qvels = kdl.JntArrayVel(kdl_jnt_angles, kdl_jnt_vels)
        idx = self.arm_link_names.index(tgt_frame) + 1
        fg = self.fk_solver_vel.JntToCart(kdl_jnt_qvels,
                                          kdl_end_frame,
                                          idx)
        if fg == 0:
            raise ValueError('KDL Vel JntToCart error!')
        end_twist = kdl_end_frame.GetTwist()
        return np.array([end_twist[0], end_twist[1], end_twist[2],
                         end_twist[3], end_twist[4], end_twist[5]])

    def compute_ik(self, pos, ori=None, qinit=None, *args, **kwargs):
        """
        Compute the inverse kinematics solution given the
        position and orientation of the end effector
        (self.cfgs.ARM.ROBOT_EE_FRAME)

        Args:
            pos (list or np.ndarray): position (shape: [3,])
            ori (list or np.ndarray): orientation. It can be euler angles
                ([roll, pitch, yaw], shape: [4,]),
                or quaternion ([qx, qy, qz, qw], shape: [4,]),
                or rotation matrix (shape: [3, 3]). If it's None,
                the solver will use the current end effector
                orientation as the target orientation
            qinit (list or np.ndarray): initial joint positions for numerical
                IK (shape: [6,])

        Returns:
            list: inverse kinematics solution (joint angles)
        """
        if ori is not None:
            ori = np.array(ori)
            if ori.size == 3:
                # [roll, pitch, yaw]
                ee_quat = arutil.euler2quat(ori)
            elif ori.shape == (3, 3):
                ee_quat = arutil.rot2quat(ori)
            elif ori.size == 4:
                ee_quat = ori
            else:
                raise ValueError('Orientation should be rotation matrix, '
                                 'euler angles or quaternion')
        else:
            ee_pos, ee_quat, ee_rot_mat, ee_euler = self.get_ee_pose()
        ori_x = ee_quat[0]
        ori_y = ee_quat[1]
        ori_z = ee_quat[2]
        ori_w = ee_quat[3]
        if qinit is None:
            qinit = self.get_jpos().tolist()
        elif isinstance(qinit, np.ndarray):
            qinit = qinit.flatten().tolist()
        pos_tol = self.cfgs.ARM.IK_POSITION_TOLERANCE
        ori_tol = self.cfgs.ARM.IK_ORIENTATION_TOLERANCE
        jnt_poss = self.num_ik_solver.get_ik(qinit,
                                             pos[0],
                                             pos[1],
                                             pos[2],
                                             ori_x,
                                             ori_y,
                                             ori_z,
                                             ori_w,
                                             pos_tol,
                                             pos_tol,
                                             pos_tol,
                                             ori_tol,
                                             ori_tol,
                                             ori_tol)
        if jnt_poss is None:
            return None
        return list(jnt_poss)

    def _init_consts(self):
        """
        Initialize constants
        """
        self._home_position = self.cfgs.ARM.HOME_POSITION

        robot_description = self.cfgs.ROBOT_DESCRIPTION
        urdf_string = rospy.get_param(robot_description)
        self.num_ik_solver = trac_ik.IK(self.cfgs.ARM.ROBOT_BASE_FRAME,
                                        self.cfgs.ARM.ROBOT_EE_FRAME,
                                        urdf_string=urdf_string)
        _, self.urdf_tree = treeFromParam(robot_description)
        base_frame = self.cfgs.ARM.ROBOT_BASE_FRAME
        ee_frame = self.cfgs.ARM.ROBOT_EE_FRAME
        self.urdf_chain = self.urdf_tree.getChain(base_frame,
                                                  ee_frame)
        self.arm_jnt_names = self._get_kdl_joint_names()
        self.arm_jnt_names_set = set(self.arm_jnt_names)
        self.arm_link_names = self._get_kdl_link_names()
        self.arm_dof = len(self.arm_jnt_names)
        self.gripper_tip_pos, self.gripper_tip_ori = self._get_tip_transform()

        moveit_commander.roscpp_initialize(sys.argv)
        self.moveit_group = MoveGroupCommander(self.cfgs.ARM.MOVEGROUP_NAME)
        self.moveit_group.set_planner_id(self.moveit_planner)
        self.moveit_group.set_planning_time(1.0)
        self.moveit_scene = MoveitScene()
        self._scale_moveit_motion(vel_scale=0.2, acc_scale=0.2)

        # add a virtual base support frame of the real robot:
        ur_base_name = 'ur_base'
        ur_base_attached = False
        for _ in range(2):
            self.moveit_scene.add_static_obj(
                ur_base_name,
                [0, 0, -0.5],
                [0, 0, 0, 1],
                size=[0.25, 0.50, 1.0],
                obj_type='box',
                ref_frame=self.cfgs.ARM.ROBOT_BASE_FRAME)
            time.sleep(1)
            obj_dict, obj_adict = self.moveit_scene.get_objects()
            if ur_base_name in obj_dict.keys():
                ur_base_attached = True
                break
        if not ur_base_attached:
            print_red('Fail to add the UR base support as a collision object. '
                      'Be careful when you use moveit to plan the path! You'
                      'can try again to add the base manually.')

        # add a virtual bounding box for the wrist mounted camera
        wrist_cam_name = 'wrist_cam'
        wrist_cam_attached = False
        safe_camera_links = [
            'wrist_3_link',
            'ee_link',
            'robotiq_arg2f_coupling',
            'robotiq_arg2f_base_link'
        ]
        for _ in range(2):
            self.moveit_scene.add_dynamic_obj(
                'robotiq_arg2f_base_link',
                wrist_cam_name,
                [0.0375, 0, 0],
                [0, 0, 0, 1],
                [0.03, 0.1, 0.03],
                touch_links=safe_camera_links)
            time.sleep(1)
            obj_dict, obj_adict = self.moveit_scene.get_objects()
            if wrist_cam_name in obj_adict.keys():
                wrist_cam_attached = True
                break
        if not wrist_cam_attached:
            print_red('Fail to add the wrist camera bounding box as collision'
                      'object. Be careful when you use moveit to plan paths!'
                      'You can try again to add the camera box manually.')

        self.jac_solver = kdl.ChainJntToJacSolver(self.urdf_chain)
        self.fk_solver_pos = kdl.ChainFkSolverPos_recursive(self.urdf_chain)
        self.fk_solver_vel = kdl.ChainFkSolverVel_recursive(self.urdf_chain)

        self.ee_link = self.cfgs.ARM.ROBOT_EE_FRAME

        self._j_pos = dict()
        self._j_vel = dict()
        self._j_torq = dict()
        self._j_state_lock = threading.RLock()
        self.tf_listener = tf.TransformListener()
        rospy.Subscriber(self.cfgs.ARM.ROSTOPIC_JOINT_STATES, JointState,
                         self._callback_joint_states)
        # https://www.universal-robots.com/how-tos-and-faqs/faq/ur-faq/max-joint-torques-17260/
        self._max_torques = [150, 150, 150, 28, 28, 28]

    def _send_urscript(self, prog):
        """
        Method to send URScript program to the URScript ROS topic

        Args:
            prog (str): URScript program which will be sent and run on
                the UR5e machine

        """
        # TODO return the status info
        # such as if the robot gives any error,
        # the execution is successful or not

        self.urscript_pub.publish(prog)

    def _output_pendant_msg(self, msg):
        """
        Method to display a text message on the UR5e teach pendant

        Args:
            msg (str): message to display

        Return:
            None
        """
        prog = 'textmsg(%s)' % msg
        self._send_urscript(prog)

    def _scale_moveit_motion(self, vel_scale=1.0, acc_scale=1.0):
        """
        Sets the maximum velocity and acceleration MoveIt can set
        in the trajectories it returns. Specified as a fraction
        from 0.0 - 1.0 of the maximum velocity and acceleration
        specified in the MoveIt joint limits configuration file.

        Keyword Arguments:
            vel_scale (float): Defaults to 1.0
            acc_scale (float): Defaults to 1.0
        """
        vel_scale = arutil.clamp(vel_scale, 0.0, 1.0)
        acc_scale = arutil.clamp(acc_scale, 0.0, 1.0)
        self.moveit_group.set_max_velocity_scaling_factor(vel_scale)
        self.moveit_group.set_max_acceleration_scaling_factor(acc_scale)

    def _callback_joint_states(self, msg):
        """
        ROS subscriber callback for arm joint states

        Args:
            msg (sensor_msgs/JointState): Contains message published in topic
        """
        self._j_state_lock.acquire()
        for idx, name in enumerate(msg.name):
            if name in self.arm_jnt_names:
                if idx < len(msg.position):
                    self._j_pos[name] = msg.position[idx]
                if idx < len(msg.velocity):
                    self._j_vel[name] = msg.velocity[idx]
                if idx < len(msg.effort):
                    self._j_torq[name] = msg.effort[idx]
        self._j_state_lock.release()

    def _get_kdl_link_names(self):
        """
        Internal method to get the link names from the KDL URDF chain

        Returns:
            list: List of link names
        """
        num_links = self.urdf_chain.getNrOfSegments()
        link_names = []
        for i in range(num_links):
            link_names.append(self.urdf_chain.getSegment(i).getName())
        return copy.deepcopy(link_names)

    def _get_kdl_joint_names(self):
        """
        Internal method to get the joint names from the KDL URDF chain

        Returns:
            list: List of joint names
        """
        num_links = self.urdf_chain.getNrOfSegments()
        num_joints = self.urdf_chain.getNrOfJoints()
        joint_names = []
        for i in range(num_links):
            link = self.urdf_chain.getSegment(i)
            joint = link.getJoint()
            joint_type = joint.getType()
            # JointType definition: [RotAxis,RotX,RotY,RotZ,TransAxis,
            #                        TransX,TransY,TransZ,None]
            if joint_type > 1:
                continue
            joint_names.append(joint.getName())
        assert num_joints == len(joint_names)
        return copy.deepcopy(joint_names)

    def _get_tip_transform(self):
        """
        Internal method to get the transform between the robot's
        wrist and the tip of the gripper

        Returns:
            list: Translation component of the gripper tip transform
                (shape [3,])
            list: Euler angle orientation component of the gripper
                tip transform. (shape [3,])
        """
        ee_frame = self.cfgs.ARM.ROBOT_EE_FRAME
        gripper_tip_id = self.arm_link_names.index(ee_frame)
        gripper_tip_link = self.urdf_chain.getSegment(gripper_tip_id)
        gripper_tip_tf = kdl_frame_to_numpy(gripper_tip_link.getFrameToTip())
        gripper_tip_pos = gripper_tip_tf[:3, 3].flatten()
        gripper_tip_rot_mat = gripper_tip_tf[:3, :3]
        gripper_tip_euler = arutil.rot2euler(gripper_tip_rot_mat)
        return list(gripper_tip_pos), list(gripper_tip_euler)

    def _set_tool_offset(self):
        """
        Internal method to send a URScript command to the robot so that
        it updates it tool center point variable to match the URDF
        """
        tool_offset_prog = 'set_tcp(p[%f, %f, %f, %f, %f, %f])' % (
            self.gripper_tip_pos[0],
            self.gripper_tip_pos[1],
            self.gripper_tip_pos[2],
            self.gripper_tip_ori[0],
            self.gripper_tip_ori[1],
            self.gripper_tip_ori[2]
        )

        self._output_pendant_msg(tool_offset_prog)
        self._send_urscript(tool_offset_prog)

    def _pub_joint_vel(self, velocity):
        """
        Received joint velocity command, puts it in a ROS trajectory
        msg, and uses internal publisher to send to /joint_speed topic
        on the real robot

        Args:
            velocity (list): List of desired joint velocities
                (length and order have already been checked)
        """
        goal_speed_msg = JointTrajectory()
        goal_speed_msg.points.append(
            JointTrajectoryPoint(
                velocities=velocity))
        self.joint_vel_pub.publish(goal_speed_msg)

    def _setup_pub_sub(self):
        """
        Initialize all the publishers and subscribers used internally
        """
        # for publishing joint speed to real robot
        self.joint_vel_pub = rospy.Publisher(
            self.cfgs.ARM.JOINT_SPEED_TOPIC,
            JointTrajectory,
            queue_size=2
        )

        self.urscript_pub = rospy.Publisher(
            self.cfgs.ARM.ARM.URSCRIPT_TOPIC,
            String,
            queue_size=10
        )

        rospy.sleep(2.0)
