# -*- coding: utf-8 -*-
"""
Created on Sun Nov 19 23:08:18 2017

@author: cz
"""

try:
    import cv2
    USE_CV2 = True
except ImportError:
    USE_CV2 = False

import operator
import math
from state import State
import numpy as np
import vrep
from data import Data
from pointcloud import PointCloud
import time


LIMIT_MAX_ACC = False
accMax = 0.5 # m/s^2

def saturate(dxp, dyp, dxypMax):
    dxyp = (dxp**2 + dyp**2)**0.5
    if dxyp > dxypMax:
        dxp = dxp / dxyp * dxypMax
        dyp = dyp / dxyp * dxypMax
    return dxp, dyp

class Robot():
    def __init__(self, scene):
        self.scene = scene
        self.dynamics = 1
        
        # dynamics parameters
        self.l = 0.331
        
        # state
        self.xi = State(0, 0, 0, self)
        self.xid = State(3, 0, 0, self)
        self.xid0 = State(3, 0, math.pi/4, self)
        self.reachedGoal = False
        # Control parameters
        self.kRho = 1
        self.kAlpha = 6
        self.kPhi = -1
        self.kV = 3.8
        self.gamma = 0.15
        
        #
        self.pointCloud = PointCloud(self)
        
        
        # Data to be recorded
        self.recordData = False
        self.data = Data(self)
        self.v1Desired = 0
        self.v2Desired = 0

        self.role = None
        self.neighbors = []
        self.leader = None # Only for data recording purposes        
       
        self.ctrl1_sm = []
        self.ctrl2_sm = []
        
    def propagateDesired(self):
        if self.dynamics == 5:
            pass
        elif self.dynamics == 4 or self.dynamics == 11:
            # Circular desired trajectory, depricated.
            t = self.scene.t
            radius = 2
            omega = 0.2
            theta0 = math.atan2(self.xid0.y, self.xid0.x)
            rho0 = (self.xid0.x ** 2 + self.xid0.y ** 2) ** 0.5
            self.xid.x = (radius * math.cos(omega * t) +
                          rho0 * math.cos(omega * t + theta0))
            self.xid.y = (radius * math.sin(omega * t) +
                          rho0 * math.sin(omega * t + theta0))
            self.xid.vx = -(radius * omega * math.sin(omega * t) +
                            rho0 * omega * math.sin(omega * t + theta0))
            self.xid.vy = (radius * omega * math.cos(omega * t) +
                           rho0 * omega * math.cos(omega * t + theta0))
            self.xid.theta = math.atan2(self.xid.vy, self.xid.vx)
            #self.xid.omega = omega
            
            c = self.l/2
            self.xid.vxp = self.xid.vx - c * math.sin(self.xid.theta) * omega
            self.xid.vyp = self.xid.vy + c * math.cos(self.xid.theta) * omega
        elif self.dynamics == 16:
            # Linear desired trajectory
            t = self.scene.t
            #dt = self.scene.dt
            x = self.scene.xid.x
            y = self.scene.xid.y
            #print('x = ', x, 'y = ', y)
            theta = self.scene.xid.theta
            #print('theta = ', theta)
            sDot = self.scene.xid.sDot
            thetaDot = self.scene.xid.thetaDot
            
            phii = math.atan2(self.xid0.y, self.xid0.x)
            rhoi = (self.xid0.x ** 2 + self.xid0.y ** 2) ** 0.5
            #print('phii = ', phii)
            self.xid.x = x + rhoi * math.cos(phii)
            self.xid.y = y + rhoi * math.sin(phii)
            self.xid.vx = sDot * math.cos(theta)
            self.xid.vy = sDot * math.sin(theta)
            #print('vx: ', self.xid.vx, 'vy:', self.xid.vy)
            #print('v', self.index, ' = ', (self.xid.vx**2 + self.xid.vy**2)**0.5)
            
            dpbarx = -self.scene.xid.dpbarx
            dpbary = -self.scene.xid.dpbary
            if (dpbarx**2 + dpbary**2)**0.5 > 1e-1:
                self.xid.theta = math.atan2(dpbary, dpbarx)
            #if (self.xid.vx**2 + self.xid.vy**2)**0.5 > 1e-3:
            #    self.xid.theta = math.atan2(self.xid.vy, self.xid.vx)
            #self.xid.omega = omega
            
            c = self.l/2
            self.xid.vxp = self.xid.vx - c * math.sin(self.xid.theta) * thetaDot
            self.xid.vyp = self.xid.vy + c * math.cos(self.xid.theta) * thetaDot
            self.xid.vRef = self.scene.xid.vRef
        elif self.dynamics == 17 or self.dynamics == 18:
            self.xid.theta = self.scene.xid.vRefAng
			
			
    def precompute(self):
        self.xi.transform()
        self.xid.transform()
        self.updateNeighbors()
        
    def propagate(self):
        if self.scene.vrepConnected == False:
            self.xi.propagate(self.control)
        else:
            omega1, omega2 = self.control()
            vrep.simxSetJointTargetVelocity(self.scene.clientID, 
                                            self.motorLeftHandle, 
                                            omega1, vrep.simx_opmode_oneshot)
            vrep.simxSetJointTargetVelocity(self.scene.clientID, 
                                            self.motorRightHandle, 
                                            omega2, vrep.simx_opmode_oneshot)
            
    def updateNeighbors(self):
        self.neighbors = []
        self.leader = None
        for j in range(len(self.scene.robots)):
            if self.scene.adjMatrix[self.index, j] == 0:
                continue
            robot = self.scene.robots[j] # neighbor
            self.neighbors.append(robot)
            if robot.role == self.scene.ROLE_LEADER:
                if self.leader is not None:
                    raise Exception('There cannot be more than two leaders in a scene!')
                self.leader = robot
    
    def control(self):
        if self.learnedController is not None:
            mode = self.learnedController()
            observation, action_1 = self.data.getObservation(mode)
            if observation is None:
                action = np.array([[0, 0]])
            else:
                action = self.learnedController(observation, action_1)
            #action = np.array([[0, 0]])
            v1 = action[0, 0]
            v2 = action[0, 1]
            
            self.ctrl1_sm.append(v1)
            self.ctrl2_sm.append(v2)
            if len(self.ctrl1_sm) < 10:
                v1 = sum(self.ctrl1_sm) / len(self.ctrl1_sm)
                v2 = sum(self.ctrl2_sm) / len(self.ctrl2_sm)
            else:
                v1 = sum(self.ctrl1_sm[len(self.ctrl1_sm)-10:len(self.ctrl1_sm)]) / 10
                v2 = sum(self.ctrl2_sm[len(self.ctrl2_sm)-10:len(self.ctrl2_sm)]) / 10
                
            #print(v1,v2,'dnn')
            
        elif self.dynamics == 5:
            K3 = 0.15  # interaction between i and j
            
            # velocity in transformed space
            #vxp = 0.2
            #vyp = 0.3
            
            vxp = self.scene.xid.vxp
            vyp = self.scene.xid.vyp
            
            tauix = 0
            tauiy = 0
            for robot in self.neighbors:
                pijx = self.xi.xp - robot.xi.xp
                pijy = self.xi.yp - robot.xi.yp
                pij0 = self.xi.distancepTo(robot.xi)
                pijd0 = self.xid.distancepTo(robot.xid)
                tauij0 = 2 * (pij0**4 - pijd0**4) / pij0**3
                tauix += tauij0 * pijx / pij0
                tauiy += tauij0 * pijy / pij0
            #tauix, tauiy = saturate(tauix, tauiy, dxypMax)
            vxp += -K3 * tauix
            vyp += -K3 * tauiy
            
            self.v1Desired = vxp
            self.v2Desired = vyp
            return vxp, vyp
        
        elif self.dynamics >= 15 and self.dynamics <= 18:
            # For e-puk dynamics
            # Feedback linearization
            # v1: left wheel speed
            # v2: right wheel speed
            K3 = 0.0 # interaction between i and j
            
            dxypMax = float('inf')
            if self.role == self.scene.ROLE_LEADER: # I am a leader
                K1 = 1
                K2 = 1
            elif self.role == self.scene.ROLE_FOLLOWER:
                K1 = 0 # Reference position information is forbidden
                K2 = 1
            elif self.role == self.scene.ROLE_PEER:
                K1 = 1
                K2 = 0
                K3 = 0.15  # interaction between i and j
                dxypMax = 0.7
            
            
            # sort neighbors by distance
            if True: #not hasattr(self, 'dictDistance'):
                self.dictDistance = dict()
                for j in range(len(self.scene.robots)):
                    if self.scene.adjMatrix[self.index, j] == 0:
                        continue
                    robot = self.scene.robots[j] # neighbor
                    self.dictDistance[j] = self.xi.distancepTo(robot.xi)
                self.listSortedDistance = sorted(self.dictDistance.items(), 
                                        key=operator.itemgetter(1))
            
            # velocity in transformed space
            vxp = 0
            vyp = 0
            
            tauix = 0
            tauiy = 0
            # neighbors sorted by distances in descending order
            lsd = self.listSortedDistance
            jList = [lsd[0][0], lsd[1][0]]
            m = 2
            while m < len(lsd) and lsd[m][1] < 1.414 * lsd[1][1]:
                jList.append(lsd[m][0])
                m += 1
            #print(self.listSortedDistance)
            for j in jList: 
                robot = self.scene.robots[j]
                pijx = self.xi.xp - robot.xi.xp
                pijy = self.xi.yp - robot.xi.yp
                pij0 = self.xi.distancepTo(robot.xi)
                if self.dynamics == 18:
                    pijd0 = self.scene.alpha
                else:
                    pijd0 = self.xid.distancepTo(robot.xid)
                tauij0 = 2 * (pij0**4 - pijd0**4) / pij0**3
                tauix += tauij0 * pijx / pij0
                tauiy += tauij0 * pijy / pij0
            
            # Achieve and keep formation
            #tauix, tauiy = saturate(tauix, tauiy, dxypMax)
            vxp += -K3 * tauix
            vyp += -K3 * tauiy
            
            # Velocity control toward goal
            #dCosTheta = math.cos(self.xi.theta) - math.cos(self.xid.theta)
            #print("dCosTheta: ", dCosTheta)
            #print("theta: ", self.xi.theta, "thetad: ", self.xid.theta)
            dxp = self.scene.xid.dpbarx #+ self.l / 2 * dCosTheta
            dyp = self.scene.xid.dpbary #+ self.l / 2 * dCosTheta
            # Velocity control toward goal
            #dxp = self.xi.xp - self.xid.xp
            #dyp = self.xi.yp - self.xid.yp
            # Limit magnitude
            dxp, dyp = saturate(dxp, dyp, dxypMax)
            vxp += -K1 * dxp
            vyp += -K1 * dyp
            
            # Take goal's speed into account
            vxp += K2 * self.xid.vxp
            vyp += K2 * self.xid.vyp
            
            kk = 1
            theta = self.xi.theta
            M11 = kk * math.sin(theta) + math.cos(theta)
            M12 =-kk * math.cos(theta) + math.sin(theta)
            M21 =-kk * math.sin(theta) + math.cos(theta)
            M22 = kk * math.cos(theta) + math.sin(theta)
            
            v1 = M11 * vxp + M12 * vyp
            v2 = M21 * vxp + M22 * vyp
            
            #v1 = 0.3
            #v2 = 0.3
 
        
        
        elif self.dynamics == 20:
            # step signal
            if self.scene.t < 1:
                v1 = 0
                v2 = 0
            else:
                v1 = self.arg2[0]
                v2 = self.arg2[1]
                
        elif self.dynamics == 21:
            # step signal
            if self.scene.t < 1:
                v1 = 0
                v2 = 0
            elif self.scene.t < 4:
                v1 = self.arg2[0]
                v2 = self.arg2[1]
            elif self.scene.t < 7:
                v1 = -self.arg2[0]
                v2 = -self.arg2[1]
            else:
                v1 = self.arg2[0]
                v2 = self.arg2[1]
                
        elif self.dynamics == 22:
            # step signal
            w = 0.3
            amp = 2
            t = self.scene.t
            t0 = 1
            if t < t0:
                v1 = 0
                v2 = 0
            else:
                v1 = amp*w * math.sin(w * (t - t0)) * self.arg2[0]
                v2 = amp*w * math.sin(w * (t - t0)) * self.arg2[1]
        
        else:
            raise Exception("Undefined dynanmics")
        
        #print("v1 = %.3f" % v1, "m/s, v2 = %.3f" % v2)
        
        vm = 0.7 # wheel's max linear speed in m/s
        # Find the factor for converting linear speed to angular speed
        if math.fabs(v2) >= math.fabs(v1) and math.fabs(v2) > vm:
            alpha = vm / math.fabs(v2)
        elif math.fabs(v2) < math.fabs(v1) and math.fabs(v1) > vm:
            alpha = vm / math.fabs(v1)
        else:
            alpha = 1
        v1 = alpha * v1
        v2 = alpha * v2

        # Limit maximum acceleration (deprecated)
        
        if LIMIT_MAX_ACC:
            
            dvMax = accMax * self.scene.dt
            
            # limit v1
            dv1 = v1 - self.v1Desired
            if dv1 > dvMax:
                self.v1Desired += dvMax
            elif dv1 < -dvMax:
                self.v1Desired -= dvMax
            else:
                self.v1Desired = v1
            v1 = self.v1Desired
            
            # limit v2
            dv2 = v2 - self.v2Desired
            if dv2 > dvMax:
                self.v2Desired += dvMax
            elif dv2 < -dvMax:
                self.v2Desired -= dvMax
            else:
                self.v2Desired = v2
            v2 = self.v2Desired
        elif not LIMIT_MAX_ACC:
            self.v1Desired = v1
            self.v2Desired = v2
        
        
        # Record data
        if (self.scene.vrepConnected and 
            self.scene.SENSOR_TYPE == "VPL16" and 
            self.VPL16_counter == 3 and self.recordData == True):
            self.data.add()
        
        # print('v = ', pow(pow(v1, 2) + pow(v2, 2), 0.5))
        
        if self.scene.vrepConnected:
            omega1 = v1 * 10.25
            omega2 = v2 * 10.25
            # return angular speeds of the two wheels
            return omega1, omega2
        else:
            # return linear speeds of the two wheels
            return v1, v2
        
    def draw(self, image, drawType):
        if drawType == 1:
            xi = self.xi
            #color = (0, 0, 255)
            color = self.scene.getRobotColor(self.index, 255, True)
        elif drawType == 2:
            xi = self.xid
            color = (0, 255, 0)
        r = self.l/2
        rPix = round(r * self.scene.m2pix())
        dx = -r * math.sin(xi.theta)
        dy = r * math.cos(xi.theta)
        p1 = np.float32([[xi.x + dx, xi.y + dy]])
        p2 = np.float32([[xi.x - dx, xi.y - dy]])
        p0 = np.float32([[xi.x, xi.y]])
        p3 = np.float32([[xi.x + dy/2, xi.y - dx/2]])
        p1Pix = self.scene.m2pix(p1)
        p2Pix = self.scene.m2pix(p2)
        p0Pix = self.scene.m2pix(p0)
        p3Pix = self.scene.m2pix(p3)
        if USE_CV2 == True:
            if self.dynamics <= 1 or self.dynamics == 4 or self.dynamics == 5:
                cv2.circle(image, tuple(p0Pix[0]), rPix, color)
            else:
                cv2.line(image, tuple(p1Pix[0]), tuple(p2Pix[0]), color)
                cv2.line(image, tuple(p0Pix[0]), tuple(p3Pix[0]), color)
        
    def setPosition(self, stateVector = None):
        # stateVector = [x, y, theta]
        
        z0 = 0.1587
        if stateVector == None:
            x0 = self.xi.x
            y0 = self.xi.y
            theta0 = self.xi.theta
        elif len(stateVector) == 3:
            x0 = stateVector[0]
            y0 = stateVector[1]
            theta0 = stateVector[2]
            self.xi.x = x0
            self.xi.y = y0
            self.xi.theta = theta0
        else:
            raise Exception('Argument error!')
        if self.scene.vrepConnected == False:
            return
        vrep.simxSetObjectPosition(self.scene.clientID, self.robotHandle, -1, 
                [x0, y0, z0], vrep.simx_opmode_oneshot)
        vrep.simxSetObjectOrientation(self.scene.clientID, self.robotHandle, -1, 
                [0, 0, theta0], vrep.simx_opmode_oneshot)
        message = "Robot #" + str(self.index) + "'s pose is set to " 
        message += "[{0:.3f}, {1:.3f}, {2:.3f}]".format(x0, y0, theta0)
        self.scene.log(message)

    def readSensorData(self):
        if self.scene.vrepConnected == False:
            return
        if "readSensorData_firstCall" not in self.__dict__: 
            self.readSensorData_firstCall = True
        else:
            self.readSensorData_firstCall = False
        
        # Read robot states
        res, pos = vrep.simxGetObjectPosition(self.scene.clientID, 
                                              self.robotHandle, -1, 
                                              vrep.simx_opmode_blocking)
        if res != 0:
            raise VrepError("Cannot get object position with error code " + str(res))
        res, ori = vrep.simxGetObjectOrientation(self.scene.clientID, 
                                              self.robotHandle, -1, 
                                              vrep.simx_opmode_blocking)
        if res != 0:
            raise VrepError("Cannot get object orientation with error code " + str(res))
        res, vel, omega = vrep.simxGetObjectVelocity(self.scene.clientID,
                                                     self.robotHandle,
                                                     vrep.simx_opmode_blocking)
        if res != 0:
            raise VrepError("Cannot get object velocity with error code " + str(res))
        #print("Linear speed: %.3f" % (vel[0]**2 + vel[1]**2)**0.5, 
        #      "m/s. Angular speed: %.3f" % omega[2])
        #print("pos: %.2f" % pos[0], ", %.2f" % pos[1])
        #print("Robot #", self.index, " ori: %.3f" % ori[0], ", %.3f" % ori[1], ", %.3f" % ori[2])
        
        self.xi.x = pos[0]
        self.xi.y = pos[1]
        self.xi.alpha = ori[0]
        self.xi.beta = ori[1]
        self.xi.theta = ori[2]
        sgn = np.sign(np.dot(np.asarray(vel[0:2]), 
                             np.asarray([math.cos(self.xi.theta), 
                                         math.sin(self.xi.theta)])))
        self.vActual = sgn * (vel[0]**2 + vel[1]**2)**0.5
        self.omegaActual = omega[2]
        # Read laser/vision sensor data
        if self.scene.SENSOR_TYPE == "2d_":
            # self.laserFrontHandle
            # self.laserRearHandle
            
            if self.readSensorData_firstCall:
                opmode = vrep.simx_opmode_streaming
            else:
                opmode = vrep.simx_opmode_buffer
            laserFront_points = vrep.simxGetStringSignal(
                    self.scene.clientID, self.laserFrontName + '_signal', opmode)
            print(self.laserFrontName + '_signal: ', len(laserFront_points[1]))
            laserRear_points = vrep.simxGetStringSignal(
                    self.scene.clientID, self.laserRearName + '_signal', opmode)
            print(self.laserRearName + '_signal: ', len(laserRear_points[1]))
        elif self.scene.SENSOR_TYPE == "2d": # deprecated
            raise Exception('2d sensor is not supported!!!!')
        elif self.scene.SENSOR_TYPE == "VPL16":
            # self.pointCloudHandle
            velodyne_points = vrep.simxCallScriptFunction(
                    self.scene.clientID, self.pointCloudName, 1, 
                    'getVelodyneData_function', [], [], [], 'abc', 
                    vrep.simx_opmode_blocking)
            #print(len(velodyne_points[2]))
            #print(velodyne_points[2])
            res = velodyne_points[0]
            
            # Parse data
            if 'VPL16_counter' not in self.__dict__:
                self.VPL16_counter = 0
            # reset the counter every fourth time
            if self.VPL16_counter == 4:
                self.VPL16_counter = 0
            if self.VPL16_counter == 0:
                # Reset point cloud
                self.pointCloud.clearData()
            #print('VPL16_counter = ', self.VPL16_counter)
            self.pointCloud.addRawData(velodyne_points[2]) # will rotate here
            
            if self.VPL16_counter == 3:
                
                #print("Length of point cloud is " + str(len(self.pointCloud.data)))
                if res != 0:
                    raise VrepError("Cannot get point cloud with error code " + str(res))
                
                #start = time.clock()
                self.pointCloud.crop()
                #end = time.clock()
                #self.pointCloud.updateScanVector() # option 2
                self.pointCloud.updateOccupancyMap() # option 1
                #print('Time elapsed: ', end - start)
            self.VPL16_counter += 1
            
        elif self.scene.SENSOR_TYPE == "kinect":
            pass
        else:
            return
    def getV1V2(self):
        v1 = self.vActual + self.omegaActual * self.l / 2
        v2 = self.vActual - self.omegaActual * self.l / 2
        return np.array([[v1, v2]])
        


       
class VrepError(Exception):
    # Exception raised for errors related vrep.

    def __init__(self, message):
        self.message = message
        