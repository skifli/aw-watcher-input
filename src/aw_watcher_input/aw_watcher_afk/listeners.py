"""
Listeners for aggregated keyboard and mouse events.

This is used for AFK detection on Linux, as well as used in aw-watcher-input to track input activity in general.

NOTE: Logging usage should be commented out before committed, for performance reasons.
"""

import logging
import threading
import struct
import os
import sys
import time
import subprocess
import select
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from typing import Dict, Any, List

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Fix pynput's xorg handler bug before importing Listener
def _patch_pynput_xorg():
    """Patch pynput's xorg.Listener to fix Python 3.13 ThreadHandle bug"""
    try:
        # Import the xorg listener to trigger patching
        from pynput._util.xorg import Listener as XorgListener
        
        # Store the original __init__
        _original_xorg_init = XorgListener.__init__
        
        def patched_xorg_init(self, *args, **kwargs):
            # Call original init
            _original_xorg_init(self, *args, **kwargs)
            
            # Now fix the _display_stop issue by wrapping the problem away
            if hasattr(self, '_display_stop'):
                original_display_stop = self._display_stop
                self._display_stop = None  # Prevent the callable bug
                self._original_display_stop = original_display_stop
        
        # Apply the patch
        XorgListener.__init__ = patched_xorg_init
    except Exception as e:
        logger.debug(f"Failed to patch pynput xorg: {e}")

_patch_pynput_xorg()


class EventFactory(metaclass=ABCMeta):
    def __init__(self) -> None:
        self.new_event = threading.Event()
        self._reset_data()

    @abstractmethod
    def _reset_data(self) -> None:
        self.event_data: Dict[str, Any] = {}

    def next_event(self) -> dict:
        """Returns an event and prepares the internal state so that it can start to build a new event"""
        self.new_event.clear()
        data = self.event_data
        # self.logger.debug(f"Event: {data}")
        self._reset_data()
        return data

    def has_new_event(self) -> bool:
        return self.new_event.is_set()


class KeyboardListener(EventFactory):
    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("keyboard")
        self._listener = None
        self._stop_event = threading.Event()
        self._thread = None
        self._use_devinput = False
        self._use_linux_monitor = False
        self._linux_cursor = 0

    def start(self):
        if sys.platform.startswith("linux"):
            try:
                from .linux_input import start_monitor

                self._use_devinput = True
                self._use_linux_monitor = True
                self._stop_event.clear()
                start_monitor()
                self.logger.debug("Using shared Linux input monitor for keyboard")
                return
            except Exception as e:
                self.logger.debug(
                    f"Shared Linux input monitor unavailable: {e}, falling back"
                )

        # Skip pynput on Python 3.13 - use direct /dev/input reading instead
        # pynput's xorg module doesn't deliver events on Python 3.13+
        if sys.version_info >= (3, 13):
            self.logger.debug("Python 3.13+: Using /dev/input fallback directly (pynput broken)")
            self._use_devinput = True
            self._start_devinput()
        else:
            try:
                from pynput import keyboard
                self._listener = keyboard.Listener(
                    on_press=self.on_press, on_release=self.on_release
                )
                self._listener.start()
                self.logger.debug("Using pynput keyboard listener")
            except Exception as e:
                self.logger.debug(f"pynput failed: {e}, trying /dev/input fallback")
                self._use_devinput = True
                self._start_devinput()

    def _start_devinput(self):
        """Fallback: read keyboard events directly from /dev/input/"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_devinput, daemon=True)
        self._thread.start()

    def _read_devinput(self):
        """Read keyboard events from /dev/input/ devices"""
        import glob
        
        try:
            # Find all event devices
            event_devices = glob.glob('/dev/input/event*')
            self.logger.debug(f"Found input devices: {event_devices}")
            
            open_devices = []
            try:
                for device_path in event_devices:
                    try:
                        # Check if this device has keyboard events (EV_KEY capability)
                        try:
                            # Try to read capability bits from /sys/class/input/eventX/device/capabilities/
                            device_name = os.path.basename(device_path)
                            cap_path = f"/sys/class/input/{device_name}/device/capabilities/ev"
                            if os.path.exists(cap_path):
                                with open(cap_path, 'r') as f:
                                    cap_val = int(f.read().strip(), 16)
                                    if not (cap_val & 0x1):  # Check EV_KEY bit
                                        self.logger.debug(f"Skipping {device_path} - no EV_KEY")
                                        continue
                        except:
                            pass  # If we can't check capabilities, try to open anyway
                        
                        # Open in BLOCKING mode so we wait for events
                        fd = os.open(device_path, os.O_RDONLY)
                        open_devices.append((fd, device_path))
                    except (OSError, PermissionError) as e:
                        self.logger.debug(f"Cannot open {device_path}: {e}")
                
                if not open_devices:
                    self.logger.warning("No keyboard input devices found - falling back to TTY monitoring")
                    return
                    
                self.logger.info(f"Monitoring {len(open_devices)} keyboard input devices")
                
                # Extract just the file descriptors for select()
                fds = [fd for fd, _ in open_devices]
                
                # Track time to detect if /dev/input is actually working
                no_events_count = 0
                last_event_time = time.time()
                
                while not self._stop_event.is_set():
                    if not fds:
                        self.logger.warning("No readable keyboard devices remaining")
                        break
                    # Use select to wait for data with a timeout
                    try:
                        ready, _, _ = select.select(fds, [], [], 0.1)
                    except:
                        ready = []
                    
                    # If no events after 10 seconds, reset counter (don't switch backends mid-thread)
                    if not ready:
                        no_events_count += 1
                    else:
                        no_events_count = 0
                        last_event_time = time.time()
                    
                    for fd in ready:
                        # Find the device path for this fd
                        device_path = next((p for f, p in open_devices if f == fd), 'unknown')
                        try:
                            # Read input_event structures (24 bytes each on 64-bit)
                            data = os.read(fd, 24)
                            if len(data) == 24:
                                # Unpack: 2 longs (time.tv_sec, time.tv_usec), unsigned short (type), unsigned short (code), int (value)
                                event_data = struct.unpack('llHHi', data)
                                time_sec, time_usec, ev_type, code, value = event_data
                                
                                self.logger.debug(f"Raw event: type={ev_type} code={code} value={value}")
                                
                                # EV_KEY = 1
                                if ev_type == 1:
                                    # KEY_PRESS = 1, KEY_RELEASE = 0
                                    if value == 1:
                                        self.event_data["presses"] += 1
                                        self.new_event.set()
                                        self.logger.info(f"Key press detected")
                        except (BlockingIOError, OSError) as e:
                            if isinstance(e, OSError) and e.errno in (19, 9):
                                self.logger.info(
                                    "Keyboard device disconnected: %s", device_path
                                )
                                if fd in fds:
                                    fds.remove(fd)
                                open_devices = [d for d in open_devices if d[0] != fd]
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                                continue
                            self.logger.debug(f"Read error on {device_path}: {e}")
                    
            finally:
                for fd, _ in open_devices:
                    try:
                        os.close(fd)
                    except:
                        pass
        except Exception as e:
            self.logger.error(f"Error in keyboard _read_devinput: {e}", exc_info=True)

    def _read_tty_activity(self):
        """Fallback: Detect keyboard activity via display server (X11/Wayland) or process activity"""
        self.logger.info("Using display/process activity monitoring for keyboard detection")
        
        # First, try X11 display server activity detection
        if self._try_x11_display_activity():
            return

        # Wayland / compositor-friendly fallback via libinput debug-events
        if self._try_libinput_activity():
            return
        
        # Fallback to process I/O activity
        self._read_io_activity()
    
    def _try_x11_display_activity(self):
        """Try to detect keyboard activity via X11 input events."""
        display = os.environ.get('DISPLAY')
        if not display:
            self.logger.debug("No DISPLAY set; skipping X11 monitoring")
            return False

        try:
            proc = subprocess.Popen(
                ['xinput', 'test-xi2', '--root'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env={**os.environ, 'DISPLAY': display},
            )
        except (FileNotFoundError, OSError) as e:
            self.logger.debug(f"xinput not available for X11 monitoring: {e}")
            return False

        self.logger.info("Monitoring X11 display for keyboard activity via xinput")
        if proc.stdout is None:
            proc.terminate()
            return False

        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                if '(KeyPress)' in line:
                    self.event_data["presses"] += 1
                    self.new_event.set()
                    self.logger.debug(f"X11 keyboard event: {line}")
                elif '(ButtonPress)' in line:
                    self.event_data["presses"] += 1
                    self.new_event.set()
                    self.logger.debug(f"X11 pointer button event: {line}")
                elif '(Motion)' in line:
                    self.event_data["deltaX"] += 1
                    self.event_data["deltaY"] += 1
                    self.new_event.set()
                    self.logger.debug(f"X11 motion event: {line}")

            return True
        except Exception as e:
            self.logger.debug(f"X11 display monitoring failed: {e}")
            return False
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
    
    def _read_io_activity(self):
        """Fallback: Detect activity via process I/O stats"""
        last_io_stats = {}
        
        # Get initial I/O stats
        try:
            with open(f'/proc/{os.getpid()}/io', 'r') as f:
                for line in f:
                    if 'read_bytes' in line:
                        last_io_stats['read'] = int(line.split(':')[1].strip())
                    elif 'write_bytes' in line:
                        last_io_stats['write'] = int(line.split(':')[1].strip())
        except:
            self.logger.warning("Cannot read /proc/self/io - activity detection won't work")
            return
        
        self.logger.info("Using process I/O monitoring for keyboard detection")
        
        while not self._stop_event.is_set():
            try:
                # Check if there's been any I/O activity
                with open(f'/proc/{os.getpid()}/io', 'r') as f:
                    for line in f:
                        if 'read_bytes' in line:
                            current = int(line.split(':')[1].strip())
                            if current > last_io_stats.get('read', 0):
                                self.event_data["presses"] += 1
                                self.new_event.set()
                                self.logger.debug("Detected I/O read activity (simulated keypress)")
                                last_io_stats['read'] = current
                        elif 'write_bytes' in line:
                            current = int(line.split(':')[1].strip())
                            if current > last_io_stats.get('write', 0):
                                self.event_data["presses"] += 1
                                self.new_event.set()
                                self.logger.debug("Detected I/O write activity (simulated keypress)")
                                last_io_stats['write'] = current
                self._stop_event.wait(0.1)
            except:
                self._stop_event.wait(0.5)

    def _refresh_from_linux_monitor(self):
        if not self._use_linux_monitor:
            return

        from .linux_input import snapshot

        data, new_cursor, has_new = snapshot("keyboard", self._linux_cursor)
        if has_new:
            self._linux_cursor = new_cursor
            self.event_data["presses"] += data.get("presses", 0)
            self.new_event.set()

    def _try_libinput_activity(self):
        """Try to detect keyboard activity via libinput debug-events."""
        try:
            proc = subprocess.Popen(
                ['libinput', 'debug-events', '--udev', 'seat0'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as e:
            self.logger.debug(f"libinput not available for keyboard monitoring: {e}")
            return False

        self.logger.info("Monitoring input via libinput debug-events for keyboard activity")
        if proc.stdout is None:
            proc.terminate()
            return False

        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                if 'KEYBOARD_KEY' in line and 'pressed' in line:
                    self.event_data["presses"] += 1
                    self.new_event.set()
                    self.logger.debug(f"libinput keyboard event: {line}")
                elif 'POINTER_BUTTON' in line and 'pressed' in line:
                    self.event_data["presses"] += 1
                    self.new_event.set()
                    self.logger.debug(f"libinput pointer button event: {line}")

            return True
        except Exception as e:
            self.logger.debug(f"libinput keyboard monitoring failed: {e}")
            return False
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            
            self._stop_event.wait(0.1)

    def stop(self):
        if self._use_linux_monitor:
            self._stop_event.set()
            try:
                from .linux_input import stop_monitor

                stop_monitor()
            except Exception:
                pass
        elif self._use_devinput:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=1.0)
        elif self._listener is not None:
            try:
                self._listener.stop()
            except TypeError:
                # Suppress pynput ThreadHandle errors on cleanup (Python 3.13 compatibility)
                pass

    def is_alive(self) -> bool:
        if self._use_linux_monitor:
            try:
                from .linux_input import monitor_alive

                return monitor_alive()
            except Exception:
                return False
        if self._use_devinput:
            return self._thread is not None and self._thread.is_alive()
        return self._listener is not None and self._listener.is_alive()

    def _reset_data(self):
        self.event_data = {"presses": 0}

    def on_press(self, key):
        try:
            self.logger.debug(f"Press: {key}")
            self.event_data["presses"] += 1
            self.new_event.set()
        except Exception as e:
            self.logger.debug(f"Error in on_press: {e}")

    def has_new_event(self) -> bool:
        self._refresh_from_linux_monitor()
        return super().has_new_event()

    def next_event(self) -> dict:
        self._refresh_from_linux_monitor()
        return super().next_event()

    def on_release(self, key):
        try:
            # Don't count releases, only clicks
            # self.logger.debug(f"Release: {key}")
            pass
        except Exception as e:
            self.logger.debug(f"Error in on_release: {e}")


class MouseListener(EventFactory):
    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("mouse")
        self.pos = None
        self._listener = None
        self._stop_event = threading.Event()
        self._thread = None
        self._use_devinput = False
        self._use_linux_monitor = False
        self._linux_cursor = 0

    def _reset_data(self):
        self.event_data = defaultdict(int)
        self.event_data.update(
            {"clicks": 0, "deltaX": 0, "deltaY": 0, "scrollX": 0, "scrollY": 0}
        )

    def start(self):
        if sys.platform.startswith("linux"):
            try:
                from .linux_input import start_monitor

                self._use_devinput = True
                self._use_linux_monitor = True
                self._stop_event.clear()
                start_monitor()
                self.logger.debug("Using shared Linux input monitor for mouse")
                return
            except Exception as e:
                self.logger.debug(
                    f"Shared Linux input monitor unavailable: {e}, falling back"
                )

        # Skip pynput on Python 3.13 - use direct /dev/input reading instead
        # pynput's xorg module doesn't deliver events on Python 3.13+
        if sys.version_info >= (3, 13):
            self.logger.debug("Python 3.13+: Using /dev/input fallback directly (pynput broken)")
            self._use_devinput = True
            self._start_devinput()
        else:
            try:
                from pynput import mouse
                self._listener = mouse.Listener(
                    on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll
                )
                self._listener.start()
                self.logger.debug("Using pynput mouse listener")
            except Exception as e:
                self.logger.debug(f"pynput failed: {e}, trying /dev/input fallback")
                self._use_devinput = True
                self._start_devinput()

    def _start_devinput(self):
        """Fallback: read mouse events directly from /dev/input/"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_devinput, daemon=True)
        self._thread.start()

    def _read_devinput(self):
        """Read mouse events from /dev/input/ devices, fall back to X11 display monitoring"""
        import glob
        import select
        
        try:
            # Find all event devices
            event_devices = glob.glob('/dev/input/event*')
            self.logger.debug(f"Found input devices: {event_devices}")
            
            open_devices = []
            try:
                for device_path in event_devices:
                    try:
                        # Open in BLOCKING mode so we wait for events
                        fd = os.open(device_path, os.O_RDONLY)
                        open_devices.append((fd, device_path))
                    except (OSError, PermissionError) as e:
                        self.logger.debug(f"Cannot open {device_path}: {e}")
                
                if not open_devices:
                    self.logger.warning("No mouse input devices found - falling back to X11/libinput monitoring")
                    if self._read_x11_mouse_activity():
                        return
                    if self._read_libinput_mouse_activity():
                        return
                    return
                    
                self.logger.info(f"Monitoring {len(open_devices)} input devices for mouse")
                
                # Extract just the file descriptors for select()
                fds = [fd for fd, _ in open_devices]
                
                # Track time to detect if /dev/input is actually working
                no_events_count = 0
                
                while not self._stop_event.is_set():
                    if not fds:
                        self.logger.warning("No readable mouse devices remaining")
                        break
                    # Use select to wait for data with a timeout
                    try:
                        ready, _, _ = select.select(fds, [], [], 0.1)
                    except:
                        ready = []
                    
                    # If no events after 10 seconds, just log (don't switch backends mid-thread)
                    if not ready:
                        no_events_count += 1
                        if no_events_count > 100:  # 100 * 0.1s = 10 seconds
                            if no_events_count == 101:  # Log only once
                                self.logger.warning("No /dev/input mouse events for 10 seconds - may need X11/libinput monitoring")
                            no_events_count = 101  # Cap at 101 to avoid logging repeatedly
                    else:
                        no_events_count = 0
                    
                    for fd in ready:
                        # Find the device path for this fd
                        device_path = next((p for f, p in open_devices if f == fd), 'unknown')
                        try:
                            # Read input_event structures (24 bytes each on 64-bit)
                            data = os.read(fd, 24)
                            if len(data) == 24:
                                event_data = struct.unpack('llHHi', data)
                                time_sec, time_usec, ev_type, code, value = event_data
                                
                                # EV_REL = 2 (relative movement)
                                if ev_type == 2:
                                    if code == 0:  # REL_X
                                        self.event_data["deltaX"] += abs(value)
                                        self.new_event.set()
                                    elif code == 1:  # REL_Y
                                        self.event_data["deltaY"] += abs(value)
                                        self.new_event.set()
                                    elif code == 6:  # REL_WHEEL
                                        self.event_data["scrollY"] += abs(value)
                                        self.new_event.set()
                                    elif code == 7:  # REL_HWHEEL
                                        self.event_data["scrollX"] += abs(value)
                                        self.new_event.set()
                                # EV_KEY = 1 (mouse buttons)
                                elif ev_type == 1:
                                    if code == 272:  # BTN_LEFT
                                        if value == 1:
                                            self.event_data["clicks"] += 1
                                            self.new_event.set()
                                            self.logger.debug(f"Mouse click detected")
                        except (BlockingIOError, OSError) as e:
                            if isinstance(e, OSError) and e.errno in (19, 9):
                                self.logger.info(
                                    "Mouse device disconnected: %s", device_path
                                )
                                if fd in fds:
                                    fds.remove(fd)
                                open_devices = [d for d in open_devices if d[0] != fd]
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                                continue
                            self.logger.debug(f"Read error: {e}")
                    
            finally:
                for fd, _ in open_devices:
                    try:
                        os.close(fd)
                    except:
                        pass
        except Exception as e:
            self.logger.error(f"Error in mouse _read_devinput: {e}", exc_info=True)

        
    def _read_libinput_mouse_activity(self):
        """Fallback: Detect mouse activity via libinput debug-events."""
        try:
            proc = subprocess.Popen(
                ['libinput', 'debug-events', '--udev', 'seat0'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as e:
            self.logger.debug(f"libinput not available for mouse monitoring: {e}")
            return False

        self.logger.info("Monitoring input via libinput debug-events for mouse activity")
        if proc.stdout is None:
            proc.terminate()
            return False

        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                if 'POINTER_MOTION' in line or 'POINTER_MOTION_ABSOLUTE' in line:
                    self.event_data["deltaX"] += 1
                    self.event_data["deltaY"] += 1
                    self.new_event.set()
                    self.logger.debug(f"libinput mouse motion event: {line}")
                elif 'POINTER_BUTTON' in line and 'pressed' in line:
                    self.event_data["clicks"] += 1
                    self.new_event.set()
                    self.logger.debug(f"libinput mouse button event: {line}")
                elif 'POINTER_AXIS' in line:
                    self.event_data["scrollX"] += 1
                    self.event_data["scrollY"] += 1
                    self.new_event.set()
                    self.logger.debug(f"libinput mouse scroll event: {line}")

            return True
        except Exception as e:
            self.logger.debug(f"libinput mouse monitoring failed: {e}")
            return False
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    def _read_x11_mouse_activity(self):
        """Fallback: Detect mouse activity via X11 input events."""
        display = os.environ.get('DISPLAY')
        if not display:
            self.logger.debug("No DISPLAY set; skipping X11 mouse monitoring")
            return

        try:
            proc = subprocess.Popen(
                ['xinput', 'test-xi2', '--root'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env={**os.environ, 'DISPLAY': display},
            )
        except (FileNotFoundError, OSError) as e:
            self.logger.debug(f"xinput not available for X11 mouse monitoring: {e}")
            return

        self.logger.info("Monitoring X11 display for mouse activity via xinput")
        if proc.stdout is None:
            proc.terminate()
            return

        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                if '(ButtonPress)' in line:
                    self.event_data["clicks"] += 1
                    self.new_event.set()
                    self.logger.debug(f"X11 button event: {line}")
                elif '(Motion)' in line:
                    self.event_data["deltaX"] += 1
                    self.event_data["deltaY"] += 1
                    self.new_event.set()
                    self.logger.debug(f"X11 motion event: {line}")

            return
        except Exception as e:
            self.logger.error(f"Error in X11 mouse monitoring: {e}")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    def _read_tty_activity(self):
        """Fallback TTY activity detection for mouse (via I/O activity proxy)"""
        pid = os.getpid()
        io_file = f'/proc/{pid}/io'
        
        if not os.path.exists(io_file):
            self.logger.warning("Cannot monitor TTY (no /proc/io)")
            return
        
        self.logger.info("Using TTY activity detection for mouse (experimental)")
        last_rw = {'r': 0, 'w': 0}
        
        try:
            # Read initial values
            with open(io_file) as f:
                for line in f:
                    if 'read_bytes:' in line:
                        last_rw['r'] = int(line.split(':')[1].strip())
                    elif 'write_bytes:' in line:
                        last_rw['w'] = int(line.split(':')[1].strip())
            
            while not self._stop_event.is_set():
                try:
                    with open(io_file) as f:
                        curr = {'r': 0, 'w': 0}
                        for line in f:
                            if 'read_bytes:' in line:
                                curr['r'] = int(line.split(':')[1].strip())
                            elif 'write_bytes:' in line:
                                curr['w'] = int(line.split(':')[1].strip())
                    
                    # If I/O activity detected, report it as simulated clicks
                    if curr['r'] > last_rw['r'] or curr['w'] > last_rw['w']:
                        self.event_data["clicks"] += 1
                        self.new_event.set()
                        self.logger.debug(f"TTY I/O activity detected (r:{curr['r']}, w:{curr['w']})")
                    
                    last_rw = curr
                    self._stop_event.wait(0.1)
                except (IOError, ValueError) as e:
                    self.logger.debug(f"TTY activity read error: {e}")
                    self._stop_event.wait(0.5)
        except Exception as e:
            self.logger.error(f"Error in mouse TTY fallback: {e}")

    def stop(self):
        if self._use_linux_monitor:
            self._stop_event.set()
            try:
                from .linux_input import stop_monitor

                stop_monitor()
            except Exception:
                pass
        elif self._use_devinput:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=1.0)
        elif self._listener is not None:
            try:
                self._listener.stop()
            except TypeError:
                # Suppress pynput ThreadHandle errors on cleanup (Python 3.13 compatibility)
                pass

    def is_alive(self) -> bool:
        if self._use_linux_monitor:
            try:
                from .linux_input import monitor_alive

                return monitor_alive()
            except Exception:
                return False
        if self._use_devinput:
            return self._thread is not None and self._thread.is_alive()
        return self._listener is not None and self._listener.is_alive()

    def _refresh_from_linux_monitor(self):
        if not self._use_linux_monitor:
            return

        from .linux_input import snapshot

        data, new_cursor, has_new = snapshot("mouse", self._linux_cursor)
        if has_new:
            self._linux_cursor = new_cursor
            for key in ("clicks", "deltaX", "deltaY", "scrollX", "scrollY"):
                self.event_data[key] += data.get(key, 0)
            self.new_event.set()

    def on_move(self, x, y):
        try:
            newpos = (x, y)
            self.logger.debug("Moved mouse to: {},{}".format(x, y))
            if not self.pos:
                self.pos = newpos

            delta = tuple(self.pos[i] - newpos[i] for i in range(2))
            self.event_data["deltaX"] += abs(delta[0])
            self.event_data["deltaY"] += abs(delta[1])

            self.pos = newpos
            self.new_event.set()
        except Exception as e:
            self.logger.debug(f"Error in on_move: {e}")

    def on_click(self, x, y, button, down):
        try:
            self.logger.debug(f"Click: {button} at {(x, y)}")
            # Only count presses, not releases
            if down:
                self.event_data["clicks"] += 1
                self.new_event.set()
        except Exception as e:
            self.logger.debug(f"Error in on_click: {e}")

    def on_scroll(self, x, y, scrollx, scrolly):
        try:
            self.logger.debug(f"Scroll: {scrollx}, {scrolly} at {(x, y)}")
            self.event_data["scrollX"] += abs(scrollx)
            self.event_data["scrollY"] += abs(scrolly)
            self.new_event.set()
        except Exception as e:
            self.logger.debug(f"Error in on_scroll: {e}")

    def has_new_event(self) -> bool:
        self._refresh_from_linux_monitor()
        return super().has_new_event()

    def next_event(self) -> dict:
        self._refresh_from_linux_monitor()
        return super().next_event()


class GamepadListener(EventFactory):
    """Listens for gamepad/joystick button events via evdev (Linux only, optional).

    Requires the ``evdev`` package and read access to ``/dev/input/`` device files.
    On most distros users in the ``input`` group have the required access.

    Only button press events are counted (not releases), so a held button does
    not continuously trigger "not AFK".  Analog axis events are intentionally
    ignored to avoid false positives from stick drift.
    """

    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("gamepad")
        self._threads: List[threading.Thread] = []
        self._devices = []  # track open devices for cleanup in stop()
        self._stop_event = threading.Event()

    def _reset_data(self):
        self.event_data = {"buttons": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        try:
            import evdev  # noqa: F401
        except ImportError:
            self.logger.debug(
                "evdev not installed; gamepad detection unavailable. "
                "Install it with: pip install evdev"
            )
            return

        devices = self._find_gamepads()
        if not devices:
            self.logger.debug("No gamepads/joysticks found in /dev/input/")
            return

        self.logger.info(
            "Gamepad listener started for %d device(s): %s",
            len(devices),
            [d.name for d in devices],
        )
        self._stop_event.clear()
        self._devices = list(devices)  # keep references for cleanup
        for device in devices:
            t = threading.Thread(
                target=self._read_events,
                args=(device,),
                name=f"gamepad-{device.path}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop_event.set()
        # Close devices to unblock threads stuck in read_loop()
        # (the blocking select() call is released when the FD is closed)
        for device in self._devices:
            try:
                device.close()
            except (OSError, IOError):
                pass
        self._devices.clear()
        # Wait briefly for threads to finish
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

    def is_alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_gamepads(self):
        """Return all readable /dev/input/ devices that look like gamepads."""
        import evdev

        gamepads = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except (OSError, PermissionError):
                continue
            if self._is_gamepad(device):
                gamepads.append(device)
            else:
                # Close non-gamepad devices to avoid FD leaks
                device.close()
        return gamepads

    @staticmethod
    def _is_gamepad(device) -> bool:
        """Return True if *device* appears to be a gamepad or joystick."""
        import evdev

        caps = device.capabilities()
        if evdev.ecodes.EV_KEY not in caps:
            return False
        # A subset of button codes that only appear on gamepads/joysticks
        # Note: BTN_GAMEPAD == BTN_SOUTH (both 0x130) in the Linux input subsystem;
        # BTN_SOUTH is kept as the more descriptive name and BTN_GAMEPAD omitted.
        gamepad_btns = {
            evdev.ecodes.BTN_SOUTH,  # Xbox A / PS Cross (also == BTN_GAMEPAD)
            evdev.ecodes.BTN_EAST,  # Xbox B / PS Circle
            evdev.ecodes.BTN_NORTH,  # Xbox Y / PS Triangle
            evdev.ecodes.BTN_WEST,  # Xbox X / PS Square
            evdev.ecodes.BTN_JOYSTICK,  # generic joystick button
            evdev.ecodes.BTN_TRIGGER,  # joystick trigger
            evdev.ecodes.BTN_THUMB,  # joystick thumb
            evdev.ecodes.BTN_TOP,  # joystick top
        }
        device_btns = set(caps[evdev.ecodes.EV_KEY])
        return bool(device_btns & gamepad_btns)

    def _read_events(self, device) -> None:
        """Read button events from *device* until stop() is called."""
        import evdev

        try:
            for event in device.read_loop():
                if self._stop_event.is_set():
                    break
                # Count button *press* events only (value == 1)
                if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                    # self.logger.debug(f"Gamepad button press: {event.code}")
                    self.event_data["buttons"] += 1
                    self.new_event.set()
        except (OSError, IOError):
            # Device disconnected or permission lost — stop quietly
            self.logger.debug("Gamepad device %s disconnected", device.path)
        finally:
            # Always close the device FD on thread exit
            try:
                device.close()
            except (OSError, IOError):
                pass
