#!/usr/bin/env python3
from cereal import car
from math import fabs, exp
from panda import Panda

from common.conversions import Conversions as CV
from selfdrive.car import STD_CARGO_KG, create_button_event, scale_tire_stiffness, get_safety_config
from selfdrive.car.gm.radar_interface import RADAR_HEADER_MSG
from selfdrive.car.gm.values import CAR, CruiseButtons, CarControllerParams, EV_CAR, CAMERA_ACC_CAR, CanBus, \
  CC_ONLY_CAR, GMFlags
from selfdrive.car.interfaces import CarInterfaceBase, TorqueFromLateralAccelCallbackType, FRICTION_THRESHOLD
from selfdrive.controls.lib.drive_helpers import get_friction

ButtonType = car.CarState.ButtonEvent.Type
EventName = car.CarEvent.EventName
GearShifter = car.CarState.GearShifter
TransmissionType = car.CarParams.TransmissionType
NetworkLocation = car.CarParams.NetworkLocation
BUTTONS_DICT = {CruiseButtons.RES_ACCEL: ButtonType.accelCruise, CruiseButtons.DECEL_SET: ButtonType.decelCruise,
                CruiseButtons.MAIN: ButtonType.altButton3, CruiseButtons.CANCEL: ButtonType.cancel}

PEDAL_MSG = 0x201
CAM_MSG = 0x320  # AEBCmd
                 # TODO: Is this always linked to camera presence?


class CarInterface(CarInterfaceBase):
  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    return CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX

  @staticmethod
  def torque_from_lateral_accel_bolt(lateral_accel_value: float, torque_params: car.CarParams.LateralTorqueTuning,
                                     lateral_accel_error: float, lateral_accel_deadzone: float, friction_compensation: bool) -> float:
    friction = get_friction(lateral_accel_error, lateral_accel_deadzone, FRICTION_THRESHOLD, torque_params, friction_compensation)

    def sig(val):
      return 1 / (1 + exp(-val)) - 0.5

    # The "lat_accel vs torque" relationship is assumed to be the sum of "sigmoid + linear" curves
    # An important thing to consider is that the slope at 0 should be > 0 (ideally >1)
    # This has big effect on the stability about 0 (noise when going straight)
    # ToDo: To generalize to other GMs, explore tanh function as the nonlinear
    a, b, c, _ = [2.6531724862969748, 1.0, 0.1919764879840985, 0.009054123646805178]  # weights computed offline

    steer_torque = (sig(lateral_accel_value * a) * b) + (lateral_accel_value * c)
    return float(steer_torque) + friction

  def torque_from_lateral_accel(self) -> TorqueFromLateralAccelCallbackType:
    if self.CP.carFingerprint in (CAR.BOLT_EUV, CAR.BOLT_CC):
      return self.torque_from_lateral_accel_bolt
    else:
      return self.torque_from_lateral_accel_linear

  @staticmethod
  def _get_params(ret, candidate, fingerprint, car_fw, experimental_long, docs):
    ret.carName = "gm"
    ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.gm)]
    ret.autoResumeSng = False
    ret.enableGasInterceptor = PEDAL_MSG in fingerprint[0]

    if candidate in EV_CAR:
      ret.transmissionType = TransmissionType.direct
    else:
      ret.transmissionType = TransmissionType.automatic

    ret.longitudinalTuning.deadzoneBP = [0.]
    ret.longitudinalTuning.deadzoneV = [0.15]

    ret.longitudinalTuning.kpBP = [5., 35.]
    ret.longitudinalTuning.kiBP = [0.]

    if candidate in CAMERA_ACC_CAR:
      ret.experimentalLongitudinalAvailable = candidate not in CC_ONLY_CAR
      ret.networkLocation = NetworkLocation.fwdCamera
      ret.radarUnavailable = True  # no radar
      ret.pcmCruise = True
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_HW_CAM
      ret.minEnableSpeed = 5 * CV.KPH_TO_MS
      ret.minSteerSpeed = 10 * CV.KPH_TO_MS

      # Tuning for experimental long
      ret.longitudinalTuning.kpV = [2.0, 1.5]
      ret.longitudinalTuning.kiV = [0.72]
      ret.stoppingDecelRate = 2.0  # reach brake quickly after enabling
      ret.stopAccel = -2.0
      ret.vEgoStopping = 0.25
      ret.vEgoStarting = 0.25

      if experimental_long:
        ret.pcmCruise = False
        ret.openpilotLongitudinalControl = True
        ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_HW_CAM_LONG

    else:  # ASCM, OBD-II harness
      ret.openpilotLongitudinalControl = True
      ret.networkLocation = NetworkLocation.gateway
      ret.radarUnavailable = RADAR_HEADER_MSG not in fingerprint[CanBus.OBSTACLE] and not docs
      ret.pcmCruise = False  # stock non-adaptive cruise control is kept off
      # supports stop and go, but initial engage must (conservatively) be above 18mph
      ret.minEnableSpeed = 18 * CV.MPH_TO_MS
      ret.minSteerSpeed = 7 * CV.MPH_TO_MS
      ret.stoppingDecelRate = 0.02

      # Tuning
      ret.longitudinalTuning.kpV = [2.4, 1.5]
      ret.longitudinalTuning.kiV = [0.36]
      if ret.enableGasInterceptor:
        # Need to set ASCM long limits when using pedal interceptor, instead of camera ACC long limits
        ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_HW_ASCM_LONG

    # Start with a baseline tuning for all GM vehicles. Override tuning as needed in each model section below.
    ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kpBP = [[0.], [0.]]
    ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.2], [0.00]]
    ret.lateralTuning.pid.kf = 0.00004   # full torque for 20 deg at 80mph means 0.00007818594
    ret.steerActuatorDelay = 0.1  # Default delay, not measured yet
    tire_stiffness_factor = 0.444  # not optimized yet

    ret.steerLimitTimer = 0.8
    ret.radarTimeStep = 0.0667  # GM radar runs at 15Hz instead of standard 20Hz
    ret.longitudinalActuatorDelayUpperBound = 0.5  # large delay to initially start braking

    if candidate in (CAR.VOLT, CAR.VOLT_CC):
      ret.minEnableSpeed = -1
      ret.mass = 1607. + STD_CARGO_KG
      ret.wheelbase = 2.69
      ret.steerRatio = 17.7  # Stock 15.7, LiveParameters
      tire_stiffness_factor = 0.469  # Stock Michelin Energy Saver A/S, LiveParameters
      ret.centerToFront = ret.wheelbase * 0.45  # Volt Gen 1, TODO corner weigh

      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
      ret.steerActuatorDelay = 0.2

      ret.longitudinalTuning.kpBP = [5., 15., 35.]
      ret.longitudinalTuning.kpV = [0.75, .9, 0.8]
      ret.longitudinalTuning.kiBP = [5., 15., 35.]
      ret.longitudinalTuning.kiV = [0.08, 0.13, 0.13]

    elif candidate == CAR.MALIBU:
      ret.mass = 1496. + STD_CARGO_KG
      ret.wheelbase = 2.83
      ret.steerRatio = 15.8
      ret.centerToFront = ret.wheelbase * 0.4  # wild guess

    elif candidate == CAR.HOLDEN_ASTRA:
      ret.mass = 1363. + STD_CARGO_KG
      ret.wheelbase = 2.662
      # Remaining parameters copied from Volt for now
      ret.centerToFront = ret.wheelbase * 0.4
      ret.steerRatio = 15.7

    elif candidate == CAR.ACADIA:
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      ret.mass = 4353. * CV.LB_TO_KG + STD_CARGO_KG
      ret.wheelbase = 2.86
      ret.steerRatio = 14.4  # end to end is 13.46
      ret.centerToFront = ret.wheelbase * 0.4
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.BUICK_LACROSSE:
      ret.mass = 1712. + STD_CARGO_KG
      ret.wheelbase = 2.91
      ret.steerRatio = 15.8
      ret.centerToFront = ret.wheelbase * 0.4  # wild guess
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.BUICK_REGAL:
      ret.mass = 3779. * CV.LB_TO_KG + STD_CARGO_KG  # (3849+3708)/2
      ret.wheelbase = 2.83  # 111.4 inches in meters
      ret.steerRatio = 14.4  # guess for tourx
      ret.centerToFront = ret.wheelbase * 0.4  # guess for tourx

    elif candidate == CAR.CADILLAC_ATS:
      ret.mass = 1601. + STD_CARGO_KG
      ret.wheelbase = 2.78
      ret.steerRatio = 15.3
      ret.centerToFront = ret.wheelbase * 0.5

    elif candidate == CAR.ESCALADE:
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      ret.mass = 5653. * CV.LB_TO_KG + STD_CARGO_KG  # (5552+5815)/2
      ret.wheelbase = 2.95  # 116 inches in meters
      ret.steerRatio = 17.3
      ret.centerToFront = ret.wheelbase * 0.5
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.ESCALADE_ESV:
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      ret.mass = 2739. + STD_CARGO_KG
      ret.wheelbase = 3.302
      ret.steerRatio = 17.3
      ret.centerToFront = ret.wheelbase * 0.5
      ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kpBP = [[10., 41.0], [10., 41.0]]
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.13, 0.24], [0.01, 0.02]]
      ret.lateralTuning.pid.kf = 0.000045
      tire_stiffness_factor = 1.0

    elif candidate in (CAR.BOLT_EUV, CAR.BOLT_CC):
      ret.mass = 1669. + STD_CARGO_KG
      ret.wheelbase = 2.63779
      ret.steerRatio = 16.8
      ret.centerToFront = ret.wheelbase * 0.4
      tire_stiffness_factor = 1.0
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

      if ret.enableGasInterceptor:
        # ACC Bolts use pedal for full longitudinal control, not just sng
        ret.flags |= GMFlags.PEDAL_LONG.value

    elif candidate == CAR.SILVERADO:
      ret.mass = 2200. + STD_CARGO_KG
      ret.wheelbase = 3.75
      ret.steerRatio = 16.3
      ret.centerToFront = ret.wheelbase * 0.5
      tire_stiffness_factor = 1.0
      # On the Bolt, the ECM and camera independently check that you are either above 5 kph or at a stop
      # with foot on brake to allow engagement, but this platform only has that check in the camera.
      # TODO: check if this is split by EV/ICE with more platforms in the future
      if ret.openpilotLongitudinalControl:
        ret.minEnableSpeed = -1.
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.EQUINOX, CAR.EQUINOX_CC):
      ret.mass = 3500. * CV.LB_TO_KG + STD_CARGO_KG
      ret.wheelbase = 2.72
      ret.steerRatio = 14.4
      ret.centerToFront = ret.wheelbase * 0.4
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.TRAILBLAZER:
      ret.mass = 1345. + STD_CARGO_KG
      ret.wheelbase = 2.64
      ret.steerRatio = 16.8
      ret.centerToFront = ret.wheelbase * 0.4
      tire_stiffness_factor = 1.0
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.SUBURBAN, CAR.SUBURBAN_CC):
      ret.mass = 2731. + STD_CARGO_KG
      ret.wheelbase = 3.302
      ret.steerRatio = 17.3 # COPIED FROM SILVERADO
      ret.centerToFront = ret.wheelbase * 0.49
      ret.steerActuatorDelay = 0.075
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.YUKON_CC:
      ret.minSteerSpeed = -1 * CV.MPH_TO_MS
      ret.mass = 5602. * CV.LB_TO_KG + STD_CARGO_KG  # (3849+3708)/2
      ret.wheelbase = 2.95  # 116 inches in meters
      ret.steerRatio = 16.3  # guess for tourx
      ret.steerRatioRear = 0.  # unknown online
      ret.centerToFront = 2.59  # ret.wheelbase * 0.4 # wild guess
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    if ret.enableGasInterceptor:
      ret.networkLocation = NetworkLocation.fwdCamera
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_HW_CAM
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_HW_CAM_LONG
      ret.minEnableSpeed = -1
      ret.pcmCruise = False
      ret.openpilotLongitudinalControl = True
      ret.stoppingControl = True
      ret.autoResumeSng = True

      if candidate in CC_ONLY_CAR:
        ret.flags |= GMFlags.PEDAL_LONG.value
        # Note: Low speed, stop and go not tested. Should be fairly smooth on highway
        ret.longitudinalTuning.kpBP = [5., 35.]
        ret.longitudinalTuning.kpV = [0.35, 0.5]
        ret.longitudinalTuning.kiBP = [0., 35.0]
        ret.longitudinalTuning.kiV = [0.1, 0.1]
        ret.longitudinalTuning.kf = 0.15
        ret.stoppingDecelRate = 0.8
      else:  # Pedal used for SNG, ACC for longitudinal control otherwise
        ret.startingState = True
        ret.vEgoStopping = 0.25
        ret.vEgoStarting = 1.0  # pedal transition speed

    elif candidate in CC_ONLY_CAR:
      ret.flags |= GMFlags.CC_LONG.value
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_CC_LONG
      ret.radarUnavailable = True
      ret.experimentalLongitudinalAvailable = False
      ret.minEnableSpeed = 24 * CV.MPH_TO_MS
      ret.openpilotLongitudinalControl = True
      ret.pcmCruise = False

    # Exception for flashed cars, or cars whose camera was removed
    if ret.networkLocation == NetworkLocation.fwdCamera and CAM_MSG not in fingerprint[CanBus.CAMERA]:
      ret.flags |= GMFlags.NO_CAMERA.value
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_GM_NO_CAMERA

    # TODO: start from empirically derived lateral slip stiffness for the civic and scale by
    # mass and CG position, so all cars will have approximately similar dyn behaviors
    ret.tireStiffnessFront, ret.tireStiffnessRear = scale_tire_stiffness(ret.mass, ret.wheelbase, ret.centerToFront,
                                                                         tire_stiffness_factor=tire_stiffness_factor)

    return ret

  # returns a car.CarState
  def _update(self, c):
    ret = self.CS.update(self.cp, self.cp_cam, self.cp_loopback)

    buttonEvents = []
    if self.CS.cruise_buttons != self.CS.prev_cruise_buttons and self.CS.prev_cruise_buttons != CruiseButtons.INIT:
      buttonEvents.append(create_button_event(self.CS.cruise_buttons, self.CS.prev_cruise_buttons, BUTTONS_DICT, CruiseButtons.UNPRESS))
      # Handle ACCButtons changing buttons mid-press
      if self.CS.cruise_buttons != CruiseButtons.UNPRESS and self.CS.prev_cruise_buttons != CruiseButtons.UNPRESS:
        buttonEvents.append(create_button_event(CruiseButtons.UNPRESS, self.CS.prev_cruise_buttons, BUTTONS_DICT, CruiseButtons.UNPRESS))
    if self.CS.distance_button_pressed:
      buttonEvents.append(car.CarState.ButtonEvent(pressed=True, type=ButtonType.gapAdjustCruise))

    ret.buttonEvents = buttonEvents

    # The ECM allows enabling on falling edge of set, but only rising edge of resume
    events = self.create_common_events(ret, extra_gears=[GearShifter.sport, GearShifter.low,
                                                         GearShifter.eco, GearShifter.manumatic],
                                       pcm_enable=self.CP.pcmCruise, enable_buttons=(ButtonType.decelCruise,))
    if not self.CP.pcmCruise:
      if any(b.type == ButtonType.accelCruise and b.pressed for b in ret.buttonEvents):
        events.add(EventName.buttonEnable)

    # Enabling at a standstill with brake is allowed
    # TODO: verify 17 Volt can enable for the first time at a stop and allow for all GMs
    below_min_enable_speed = ret.vEgo < self.CP.minEnableSpeed or self.CS.moving_backward
    if below_min_enable_speed and not (ret.standstill and ret.brake >= 20 and
                                       self.CP.networkLocation == NetworkLocation.fwdCamera):
      events.add(EventName.belowEngageSpeed)
    if ret.cruiseState.standstill and not self.CP.autoResumeSng:
      events.add(EventName.resumeRequired)
    elif ret.vEgo < self.CP.minSteerSpeed:
      events.add(EventName.belowSteerSpeed)

    if (self.CP.flags & GMFlags.CC_LONG.value) and ret.vEgo < self.CP.minEnableSpeed and ret.cruiseState.enabled:
      events.add(EventName.speedTooLow)

    if (self.CP.flags & GMFlags.PEDAL_LONG.value) and \
      self.CP.transmissionType == TransmissionType.direct and \
      not self.CS.single_pedal_mode:
      events.add(EventName.pedalInterceptorNoBrake)

    ret.events = events.to_msg()

    return ret

  def apply(self, c, now_nanos):
    return self.CC.update(c, self.CS, now_nanos)
