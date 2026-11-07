using Ryujinx.HLE;
using Ryujinx.HLE.HOS.Services.Hid;
using Ryujinx.HLE.HOS.Services.Hid.Types;
using Ryujinx.Input;
using System;
using System.Collections.Generic;
using System.Numerics;
using System.Runtime.InteropServices;
using Stopwatch = System.Diagnostics.Stopwatch;

namespace S2A.Services
{
    // 复用 HID 泵；原始双侧双频震动不经过桌面普通双马达适配器。
    internal sealed unsafe class ControllerFeedback
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct MotionSample
        {
            public ulong TimestampUs;
            public float AccelerationX, AccelerationY, AccelerationZ;
            public float GyroX, GyroY, GyroZ;
        }

        private readonly NpadDevices _npads;
        private readonly delegate* unmanaged<ulong, VibrationValue*, void> _setRumblePair;
        private readonly delegate* unmanaged<ulong, void> _stopRumble;
        private readonly delegate* unmanaged<ulong> _rumbleSession;
        private readonly delegate* unmanaged<MotionSample*, int> _readMotion;
        private readonly delegate* unmanaged<int, void> _setMotionEnabled;
        private readonly List<SixAxisInput> _motionStates = new(1);
        private readonly float[] _orientation = new float[9];
        private MotionInput _motion;
        private PlayerIndex _rumblePlayer, _motionPlayer;
        private ulong _session;
        private ulong _motionSession, _motionTimestamp;
        private long _renewAt;
        private VibrationValue _left, _right;
        private bool _motionEnabled, _motionPublished, _rumbleLogged, _motionLogged;
        private bool _rumblePermitted;
        private volatile bool _stopped;

        public static bool MotionExport(out nint motion) =>
            NativeLibrary.TryGetExport(NativeLibrary.GetMainProgramHandle(), "s2a_pad_read_motion", out motion);

        public ControllerFeedback(Switch device, PlayerIndex player)
        {
            _npads = device.Hid.Npads;
            _rumblePlayer = _motionPlayer = player;
            _rumblePermitted = _npads.VibrationPermitted;
            nint program = NativeLibrary.GetMainProgramHandle();
            if (NativeLibrary.TryGetExport(program, "s2a_pad_rumble_session", out nint rumbleSession))
            {
                // A feedback-capable host must export the atomic pair ABI; never silently split a game frame.
                _setRumblePair = (delegate* unmanaged<ulong, VibrationValue*, void>)NativeLibrary.GetExport(program, "s2a_pad_set_rumble_pair");
                _stopRumble = (delegate* unmanaged<ulong, void>)NativeLibrary.GetExport(program, "s2a_pad_stop_rumble");
                _rumbleSession = (delegate* unmanaged<ulong>)rumbleSession;
                lock (_npads.RumbleSyncRoot)
                {
                    _rumblePermitted = _npads.VibrationPermitted;
                    _session = _rumbleSession();
                    for (int i = 0; i < NpadDevices.MaxControllers; i++) _npads.ResetRumble((PlayerIndex)i);
                    _npads.RumbleBeforeAccepted = ObserveSession;
                    _npads.RumbleAccepted = AcceptRumble;
                    _npads.RumblePermissionChanged = SetRumblePermitted;
                }
            }
            if (MotionExport(out nint motion))
            {
                _readMotion = (delegate* unmanaged<MotionSample*, int>)motion;
                _setMotionEnabled = (delegate* unmanaged<int, void>)NativeLibrary.GetExport(program, "s2a_pad_set_motion_enabled");
            }
            Console.Error.WriteLine($"[输入反馈] hooks rumble_pair={_setRumblePair != null} stop={_stopRumble != null} session={_rumbleSession != null} motion={_readMotion != null} motion_enable={_setMotionEnabled != null} player={(int)player} session={_session} permit={_rumblePermitted}");
        }

        public void Update(PlayerIndex player)
        {
            if (_setRumblePair != null) UpdateRumble();
            TickMotion(player);
        }

        // All rumble state, game requests and lease renewals share Npads' existing lock.
        private void ObserveSession()
        {
            ulong session = _rumbleSession();
            if (session != _session)
            {
                Console.Error.WriteLine($"[输入反馈] state player={(int)_rumblePlayer} session={_session}->{session} permit={_rumblePermitted}");
                _session = session;
                _left = _right = default;
                _rumbleLogged = false;
                // A new session clears local state without truncating its connection cue.
                for (int i = 0; i < NpadDevices.MaxControllers; i++) _npads.ResetRumble((PlayerIndex)i);
            }
        }

        public void SetPlayer(PlayerIndex player)
        {
            lock (_npads.RumbleSyncRoot)
            {
                if (_stopped) return;
                if (_stopRumble != null) _stopRumble(_session);
                _rumblePlayer = player;
                _left = _right = default;
                _rumbleLogged = false;
                for (int i = 0; i < NpadDevices.MaxControllers; i++) _npads.ResetRumble((PlayerIndex)i);
                if (_rumbleSession != null) ObserveSession();
            }
        }

        private void SetRumblePermitted(bool permitted)
        {
            if (_stopped) return;
            ObserveSession();
            Console.Error.WriteLine($"[输入反馈] permit player={(int)_rumblePlayer} session={_session} permit={permitted}");
            if (!permitted && (Vibrating(_left) || Vibrating(_right))) _stopRumble(_session);
            _left = _right = default;
            _rumblePermitted = permitted;
        }

        // Called synchronously for each permitted game request, before Npads' queue deduplication.
        private void AcceptRumble(PlayerIndex player, VibrationValue left, VibrationValue right)
        {
            if (_stopped) return;
            if (!_rumbleLogged && (Vibrating(left) || Vibrating(right)))
            {
                _rumbleLogged = true;
                Console.Error.WriteLine($"[输入反馈] first game rumble slot={(int)player} player={(int)_rumblePlayer} session={_session} permit={_rumblePermitted} L={left.AmplitudeLow}@{left.FrequencyLow}/{left.AmplitudeHigh}@{left.FrequencyHigh} R={right.AmplitudeLow}@{right.FrequencyLow}/{right.AmplitudeHigh}@{right.FrequencyHigh}");
            }
            if (player != _rumblePlayer || _session == 0 || !_rumblePermitted) return;
            if ((left != _left || right != _right) &&
                (Vibrating(left) || Vibrating(right) || Vibrating(_left) || Vibrating(_right)))
            {
                SendPair(left, right);
                _left = left;
                _right = right;
                _renewAt = Stopwatch.GetTimestamp() + Stopwatch.Frequency / 4;
            }
        }

        private void UpdateRumble()
        {
            lock (_npads.RumbleSyncRoot)
            {
                if (_stopped) return;
                // SetPrimary supplies the authoritative player under this lock; an older HID snapshot cannot undo it.
                ObserveSession();
                long now = Stopwatch.GetTimestamp();
                if (_session != 0 && _rumblePermitted && now >= _renewAt && (Vibrating(_left) || Vibrating(_right)))
                {
                    SendPair(_left, _right);
                    _renewAt = now + Stopwatch.Frequency / 4;
                }
            }
        }

        private static bool Vibrating(VibrationValue value) => value.AmplitudeLow > 0 || value.AmplitudeHigh > 0;
        private void SendPair(VibrationValue left, VibrationValue right)
        {
            VibrationValue* values = stackalloc VibrationValue[2];
            values[0] = left;
            values[1] = right;
            _setRumblePair(_session, values);
        }

        // Only the HID thread calls this; event wakes and heartbeats may share a native sample.
        public void TickMotion(PlayerIndex player)
        {
            if (_readMotion == null || _stopped) return;
            ulong session = _rumbleSession != null ? _rumbleSession() : 0;
            if (player != _motionPlayer || session != _motionSession)
            {
                _motionPlayer = player;
                _motionSession = session;
                _motion = null;
                _motionTimestamp = 0;
                _motionLogged = false;
                if (_motionPublished)
                {
                    _motionStates.Clear();
                    _npads.UpdateSixAxis(_motionStates);
                    _motionPublished = false;
                }
            }
            bool enabled = _npads.SixAxisActive;
            if (enabled != _motionEnabled)
            {
                _setMotionEnabled(enabled ? 1 : 0);
                _motionEnabled = enabled;
            }
            _motionStates.Clear();
            MotionSample sample = default;
            int known = enabled ? _readMotion(&sample) : 0;
            if (!enabled || known == 0)
            {
                _motion = null;
                _motionTimestamp = 0;
                if (_motionPublished)
                {
                    _npads.UpdateSixAxis(_motionStates);
                    _motionPublished = false;
                }
                return;
            }
            if (sample.TimestampUs == _motionTimestamp) return;
            _motionTimestamp = sample.TimestampUs;
            if (!_motionLogged)
            {
                _motionLogged = true;
                Console.Error.WriteLine($"[输入反馈] first motion known={known} player={(int)player} timestamp_us={sample.TimestampUs} accel_g=({sample.AccelerationX},{sample.AccelerationY},{sample.AccelerationZ}) gyro_deg_s=({sample.GyroX},{sample.GyroY},{sample.GyroZ})");
            }
            // Native 已把超过 250 ms 的样本判为无效；跨会话或大间隔不积分旧姿态。
            if (_motion != null && (sample.TimestampUs < _motion.TimeStamp || sample.TimestampUs - _motion.TimeStamp > 250_000))
                _motion = null;
            _motion ??= new MotionInput();
            if (sample.TimestampUs != _motion.TimeStamp)
            {
                // NS2 raw -> SDL (x,z,-y) -> Ryujinx (x,-z,y)，合成后保留 raw 轴。
                // MotionInput 沿用上游的加速度符号、角速度积分与 Mahony 姿态融合。
                // 量程饱和与 hid 88 报给游戏的 IC 量程同一份常量（Ryujinx.HLE Types/SixAxisSensorIc.cs）：手机读数超量程的部分照真机饱和
                _motion.Update(new Vector3(SixAxisSensorIc.ClampAcceleration(sample.AccelerationX), SixAxisSensorIc.ClampAcceleration(sample.AccelerationY), SixAxisSensorIc.ClampAcceleration(sample.AccelerationZ)),
                    new Vector3(SixAxisSensorIc.ClampAngularRate(sample.GyroX), SixAxisSensorIc.ClampAngularRate(sample.GyroY), SixAxisSensorIc.ClampAngularRate(sample.GyroZ)), sample.TimestampUs, 100, 0);
            }
            Matrix4x4 m = _motion.GetOrientation();
            _orientation[0] = m.M11; _orientation[1] = m.M12; _orientation[2] = m.M13;
            _orientation[3] = m.M21; _orientation[4] = m.M22; _orientation[5] = m.M23;
            _orientation[6] = m.M31; _orientation[7] = m.M32; _orientation[8] = m.M33;
            _motionStates.Add(new SixAxisInput
            {
                PlayerId = player,
                Accelerometer = _motion.Accelerometer,
                Gyroscope = _motion.Gyroscrope * 0.0027f,
                Rotation = _motion.Rotation * 0.0027f,
                Orientation = _orientation,
            });
            _npads.UpdateSixAxis(_motionStates);
            _motionPublished = true;
        }

        public void Stop()
        {
            lock (_npads.RumbleSyncRoot)
            {
                if (_stopped) return;
                _stopped = true;
                if (_setRumblePair != null)
                {
                    _npads.RumbleBeforeAccepted = null;
                    _npads.RumbleAccepted = null;
                    _npads.RumblePermissionChanged = null;
                    _stopRumble(_session);
                }
                _left = _right = default;
                for (int i = 0; i < NpadDevices.MaxControllers; i++) _npads.ResetRumble((PlayerIndex)i);
            }
            if (_setMotionEnabled != null) _setMotionEnabled(0);
            _motion = null;
            _motionTimestamp = 0;
            _motionEnabled = false;
            if (_motionPublished)
            {
                _motionStates.Clear();
                _npads.UpdateSixAxis(_motionStates);
                _motionPublished = false;
            }
        }
    }
}
