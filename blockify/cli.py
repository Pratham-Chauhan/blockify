#!/usr/bin/env python3
"""blockify

Usage:
    blockify [-l <path>] [-v...] [-q] [-h]

Options:
    -l, --log=<path>  Enables logging to the logfile/-path specified.
    -q, --quiet       Don't print anything to stdout.
    -v                Verbosity of the logging module, up to -vvv.
    -h, --help        Show this help text.
    --version         Show current version of blockify.
"""
import json

from blockify import interludeplayer
from blockify import dbusclient
from blockify import blocklist
from gi import require_version
require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

import logging
import os
import re
import signal
import subprocess
import sys
import time

from blockify import util

log = logging.getLogger("cli")
# log.setLevel(logging.DEBUG)


from enum import Enum


class MuteMode(Enum):
    AUTO = 0
    FORCE_MUTE = 1
    FORCE_UNMUTE = 2




class Blockify(object):
    def __init__(self, blocklist):
        self.blocklist = blocklist
        self.orglist = blocklist[:]
        self.check_for_blockify_process()
        self.start_spotify_if_necessary()

        self._autodetect = util.CONFIG["general"]["autodetect"]
        self._automute = util.CONFIG["general"]["automute"]
        self.autoplay = util.CONFIG["general"]["autoplay"]
        self.unmute_delay = util.CONFIG["cli"]["unmute_delay"]
        self.update_interval = util.CONFIG["cli"]["update_interval"]
        self.spotify_refresh_interval = 2500
        self.suspend_blockify = False
        self.pulse_unmuted_value = ""
        self.song_delimiter = " - "  # u" \u2013 "
        self.found = False
        self.current_song_from_window_title = ""
        self.current_song = ""
        self.current_song_artist = ""
        self.current_song_title = ""
        self.previous_song = ""
        self.song_status = ""
        self.is_fully_muted = False
        self.is_sink_muted = False
        self.dbus = self.initialize_dbus()
        self.channels = self.initialize_channels()
        # The gst library used by interludeplayer for some reason modifies
        # argv, overwriting some of docopts functionality in the process,
        # so we import gst here, where docopts cannot be broken anymore.
        # import interludeplayer
        self.player = interludeplayer.InterludePlayer(self)

        self.initialize_mute_method()

        if self.mutemethod == self.pulse_mute:
            self.initialize_pulse_unmuted_value()

        # Only use interlude music if we use pulse sinks and the interlude playlist is non-empty.
        self.use_interlude_music = util.CONFIG["interlude"]["use_interlude_music"] and \
            self.mutemethod == self.pulsesink_mute and \
            self.player.max_index >= 0

        self.unmute_delay_timer = None

        log.info("Blockify initialized.")

    def start_spotify_if_necessary(self):
        if self.check_for_spotify_process():
            return
        log.error("No spotify process found.")

        if not util.CONFIG["general"]["start_spotify"]:
            log.info("Exiting. Bye.")
            Gtk.main_quit()

        self.start_spotify()
        if not self.check_for_spotify_process():
            log.error("Failed to start Spotify!")
            log.info("Exiting. Bye.")
            Gtk.main_quit()

    def is_localized_pulseaudio(self):
        """Pulseaudio versions below 7.0 are localized."""
        localized = False
        try:
            pulseaudio_version_string = subprocess.check_output("pulseaudio --version | awk '{print $2}'", shell=True)
            pulseaudio_version = int(pulseaudio_version_string[0])
            localized = pulseaudio_version < 7
        except Exception as e:
            log.error("Could not detect pulseaudio version: {}".format(e))

        return localized

    def initialize_pulse_unmuted_value(self):
        """Set 'no' as self.pulse_unmuted_value and try to translate if necessary."""
        unmuted_value = 'no'
        if self.is_localized_pulseaudio():
            try:
                self.install_locale()
                # Translate 'no' to the system locale.
                unmuted_value = _(unmuted_value)
            except (Exception):
                log.debug("Could not install localization. If your system "
                          "language is not english this *might* lead to unexpected "
                          "mute behaviour. A possible fix is to replace the "
                          "value of unmuted_value in blockify.py with your "
                          "translation of 'no', e.g. 'tak' in polish.")

        self.pulse_unmuted_value = unmuted_value

    def initialize_mute_method(self):
        """Determine if we can use sinks or have to use alsa."""
        try:
            devnull = open(os.devnull)
            subprocess.check_output(["pacmd", "list-sink-inputs"], stderr=devnull)
            self.mutemethod = self.pulsesink_mute
            log.debug("Mute method is pulse sink.")
        except (OSError, subprocess.CalledProcessError):
            log.debug("Mute method is alsa or pulse without sinks.")
            log.info("No pulse sinks found, falling back to system mute via alsa/pulse.")
            self.mutemethod = self.pipewire_mute

    def install_locale(self):
        import locale
        import gettext

        current_locale, encoding = locale.getdefaultlocale()
        pulseaudio_domain = 'pulseaudio'
        localedir = gettext.find(pulseaudio_domain, languages=[current_locale])
        localedir = localedir[:localedir.find('locale/')] + 'locale'
        locale = gettext.translation(pulseaudio_domain, localedir=localedir, languages=[current_locale])
        locale.install()

    def check_for_blockify_process(self):
        try:
            pid = subprocess.check_output(["pgrep", "-f", "^python.*blockify"])
        except subprocess.CalledProcessError:
            # No blockify process found. Great, this is what we want.
            pass
        else:
            if pid.strip().decode("utf-8") != str(os.getpid()):
                log.error("A blockify process is already running. Exiting.")
                Gtk.main_quit()

    def check_for_spotify_process(self):
        try:
            pidof_out = subprocess.check_output(["pidof", "spotify"])
            self.spotify_pids = pidof_out.decode("utf-8").strip().split(" ")
            return True
        except subprocess.CalledProcessError:
            return False

    def start_spotify(self):
        if util.CONFIG["general"]["start_spotify"]:
            log.info("Starting Spotify ...")
            spotify_command = "spotify"
            if util.CONFIG["general"]["detach_spotify"]:
                log.debug("Attempting to detach Spotify.")
                spotify_command += " &"
            os.system(spotify_command)
            for _ in range(20):
                time.sleep(1)
                spotify_is_running = self.check_for_spotify_process()
                if spotify_is_running:
                    log.info("Spotify launched!")
                    break

    def initialize_channels(self):
        ''' Return a list of all possible channels [Master, Speaker, Headphone] from amixer '''
        channel_list = ["Master"]
        amixer_output = subprocess.check_output("amixer", text=True)
        if "'Speaker',0" in amixer_output:
            channel_list.append("Speaker")
        if "'Headphone',0" in amixer_output:
            channel_list.append("Headphone")

        return channel_list

    def initialize_dbus(self):
        try:
            return dbusclient.DBusClient()
        except Exception as e:
            log.error("Cannot connect to DBus. Exiting.\n ({}).".format(e))
            Gtk.main_quit()

    def refresh_spotify_process_state(self):
        """Check if Spotify is running periodically. If it's not, suspend blockify."""
        previous_suspend_state = self.suspend_blockify
        if not self.check_for_spotify_process():
            self.suspend_blockify = True
        else:
            self.suspend_blockify = False

        if previous_suspend_state is not self.suspend_blockify:
            if not self.suspend_blockify:
                self.dbus.connect_to_spotify_dbus(None)
                self.player.try_resume_spotify_playback(True)
                log.warning("Spotify was restarted! Connecting now.")
            else:
                log.warning("Spotify was closed!")

        return True

    def resume_blockify(self):
        self.suspend_blockify = False
        return False

    def start(self):
        self.bind_signals()
        # Force unmute to properly initialize unmuted state
        self.toggle_mute(MuteMode.FORCE_UNMUTE)

        GLib.timeout_add(self.spotify_refresh_interval, self.refresh_spotify_process_state)

        GLib.timeout_add(self.update_interval, self.update)

        if self.autoplay:
            # Delay autoplayback until self.spotify_is_playing was called at least once.
            GLib.timeout_add(self.update_interval + 100, self.start_autoplay)

        log.info("Blockify started.")

        Gtk.main()

    def start_autoplay(self):
        if self.autoplay:
            log.debug("Autoplay is activated.")
            log.info("Starting Spotify autoplayback.")
            self.dbus.play()
        return False

    def adjust_interlude(self):
        if self.use_interlude_music:
            self.player.toggle_music()

    def spotify_is_playing(self):
        return self.song_status == "Playing"

    def update(self):
        """Main update callback function, run periodically."""
        log.debug(f'call update(): {self.current_song}')
        if not self.suspend_blockify:
            # Determine if a ad is running and act accordingly.
            self.found = self.find_ad()

            self.adjust_interlude()

        # Always return True to keep looping this method.
        return True

    def find_spotify_window(self):
        spotify_window = None
        try:
            pipe = subprocess.Popen(['wmctrl', '-lx'], stdout=subprocess.PIPE).stdout
            window_list = pipe.read().decode("utf-8").split("\n")
            for window in window_list:
                if "spotify.Spotify" in window:
                    return window
        except OSError:
            log.error("Please install wmctrl first! Exiting.")
            self.stop()

        return spotify_window

    def find_ad(self):
        """Main loop. Checks for ads and mutes accordingly."""
        self.previous_song = self.current_song
        self.update_current_song_info()

        # Manual control is enabled so we exit here.
        if not self.automute:
            return False

        if self.autodetect and self.current_song and self.current_song_is_ad():
            if self.use_interlude_music and not self.player.temp_disable:
                self.player.temp_disable = True
                log.debug("--Adding one time function callback self.player.play_with_delay() at the delay {} ms.".format(self.player.playback_delay))
                GLib.timeout_add(self.player.playback_delay, self.player.play_with_delay)

            log.debug(f"Ad found: {repr(self.current_song)}")
            self.force_mute_ad()
            return True

        # Check if the blockfile has changed.
        try:
            current_timestamp = self.blocklist.get_timestamp()
        except OSError as e:
            log.debug("Failed reading blocklist timestamp: {}. Recovering.".format(e))
            self.blocklist.__init__()
            current_timestamp = self.blocklist.timestamp
            
        if self.blocklist.timestamp != current_timestamp:
            log.info("Blockfile changed. Reloading.")
            self.blocklist.__init__()

        if self.blocklist.find(self.current_song):
            # log.info('found current song in blocklist')
            self.force_mute_ad()
            return True

        # Unmute with a certain delay to avoid the last second of commercial you sometimes hear because it's unmuted too early.
        # GLib.timeout_add(self.unmute_delay, self.unmute_with_delay)
        
        # log.debug(f'self.found: {self.found} | current song is ad? {self.current_song_is_ad()} | self.current_song: {self.current_song}')
        # if not self.found: # ad no longer playing
        self.toggle_mute(MuteMode.AUTO)
        
        return False

    def force_mute_ad(self):
        '''Force mute the ad with the configured mute method'''
        self.toggle_mute(MuteMode.FORCE_MUTE)

    def unmute_with_delay(self):
        log.debug(f">>>Unmuting with delay. is it ad? {self.found}")
        
        if not self.found:
            self.toggle_mute()
        return False

    # Audio ads typically have no artist information (via DBus) and/or "/ad/" in their spotify url.
    # Video ads have no DBus information whatsoever so they are determined via window title (wmctrl).
    def current_song_is_ad(self) -> bool:

        missing_artist = self.current_song_title and not self.current_song_artist
        has_ad_url = "/ad/" in self.dbus.get_spotify_url()

        # Since there is no reliable way to determine playback status of Spotify when not using pulseaudio,
        # we return here with a trimmed version of ad detection. At the very least, this won't mute video ads.
        if self.mutemethod != self.pulsesink_mute or not util.CONFIG["general"]["use_window_title"]:
            return missing_artist or has_ad_url

        title_mismatch = self.spotify_is_playing() and self.current_song != self.current_song_from_window_title

        # log.debug("missing_artist: {0}, has_ad_url: {1}, title_mismatch: {2}".format(missing_artist, has_ad_url,
        #                                                                             title_mismatch))

        return missing_artist or has_ad_url or title_mismatch

    def update_current_song_info(self):
        ''' get song artist and title from dbus and update current song '''
        self.current_song_artist = self.dbus.get_song_artist()
        self.current_song_title = self.dbus.get_song_title()
        self.current_song = self.current_song_artist + self.song_delimiter + self.current_song_title
        if util.CONFIG["general"]["use_window_title"]:
            self.current_song_from_window_title = self.get_current_song_from_window_title()

    def get_current_song_from_window_title(self):
        """Checks if a Spotify window exists and returns the current songname."""
        song = ""
        spotify_window = self.find_spotify_window()
        if spotify_window:
            try:
                song = " ".join(spotify_window.split()[4:])
            except Exception as e:
                log.debug("Could not extract song info from Spotify window title: {0}".format(e), exc_info=1)

        return song

    def block_current(self):
        if self.current_song:
            self.blocklist.append(self.current_song)

    def unblock_current(self):
        if self.current_song:
            if self.use_interlude_music:
                self.player.pause()
            song = self.blocklist.find(self.current_song)
            if song:
                self.blocklist.remove(song)
            else:
                log.error("Not found in blocklist or block pattern too short.")

    def toggle_mute(self, mode: MuteMode = MuteMode.AUTO):
        self.mutemethod(mode)

    def is_muted(self):      
        ''' check if any audio channel is muted (i.e volume is 0%) '''  
        for channel in self.channels:
            try:
                output = subprocess.check_output(["amixer", "get", channel],  text=True, stderr=subprocess.DEVNULL)
                if "off" in output:
                    return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        return False

    def get_state(self, mode: MuteMode):
        muted = self.is_muted()
        self.is_fully_muted = muted

        if muted: # audio is fully muted
            if mode in (MuteMode.AUTO, MuteMode.FORCE_UNMUTE) or not self.current_song:
                log.info(f"Unmuting: {self.current_song}")
                return "unmute"

        elif mode == MuteMode.FORCE_MUTE:
            log.info(f"Muting: {self.current_song}", )
            return "mute"

        return None

    def alsa_mute(self, mode: MuteMode):
        """Mute method for systems without Pulseaudio. Mutes sound system-wide."""
        state = self.get_state(mode)
        
        if not state:
            log.info('State is None.')
            return
        
        log.debug(f"Executing: amixer -q set {state}, channels: {self.channels}")
        self.update_audio_channel_state(["amixer", "-q", "set"], state)

    def pulse_mute(self, mode: MuteMode):
        """Used if pulseaudio is installed but no sinks are found. System-wide."""
        state = self.get_state(mode)
        if not state:
            return

        self.update_audio_channel_state(["amixer", "-qD", "pulse", "set"], state)


    def extract_sink_status(self, pactl_out):   
        index = ""
        for sink in pactl_out:
            sink_index = sink.get('index')
            mute = sink.get('mute')
            
            props  = sink.get('properties', {})
            app_name = props.get('application.name', '')
            if app_name == 'spotify':
                index = sink_index
                return (index, mute)
        
        return (None, None)

    def pipewire_mute(self, mode: MuteMode):
        try:
            pactl_out = subprocess.check_output(["pactl", "-f", "json", "list", "sink-inputs"], text=True) 
        except (subprocess.CalledProcessError, FileNotFoundError):
            log.error("Failed to get pactl output. is pipewire running? Resorting to alsa as mute method.")
            self.mutemethod = self.alsa_mute # Fall back to alsa mute.
            self.use_interlude_music = False
            return 
        
        sink_inputs = json.loads(pactl_out)

        index, muted_value = self.extract_sink_status(sink_inputs)
        self.is_sink_muted = muted_value

        if index:
            if self.is_sink_muted and (mode == MuteMode.FORCE_UNMUTE or not self.current_song):
                log.info("Forcing unmute.")
                subprocess.run(["pactl", "set-sink-input-mute", str(index), "0"])

            elif not self.is_sink_muted and mode == MuteMode.FORCE_MUTE:
                log.info("Muting {}.".format(self.current_song))
                subprocess.run(["pactl", "set-sink-input-mute", str(index), "1"])

            elif self.is_sink_muted and mode == MuteMode.AUTO:
                log.info("Unmuting.")
                subprocess.run(["pactl", "set-sink-input-mute", str(index), "0"])


    def update_audio_channel_state(self, command, state):
        for channel in self.channels:
            try:
                subprocess.Popen(command + [channel, state])
            except subprocess.CalledProcessError:
                pass

    def extract_pulse_sink_status(self, pacmd_out):
        sink_status = ("", "", "")  # index, playback_status, muted_value
        # Match muted_value and application.process.id values.
        pattern = re.compile(r"(?: index|state|muted|application\.process\.id).*?(\w+)")
        # Put valid spotify PIDs in a list
        output = pacmd_out.decode("utf-8")

        spotify_sink_list = [" index: " + i for i in output.split("index: ") if "spotify" in i]

        if len(spotify_sink_list) and self.spotify_pids:
            sink_infos = [pattern.findall(sink) for sink in spotify_sink_list]
            # Every third element per sublist is a key, the value is the preceding
            # two elements in the form of a tuple - {pid : (index, playback_status, muted_value)}
            idxd = {sink_status[3]: (sink_status[0], sink_status[1], sink_status[2]) for sink_status in sink_infos if
                    4 == len(sink_status)}

            pid = [k for k in idxd.keys() if k in self.spotify_pids][0]
            sink_status = idxd[pid]

        return sink_status

    def pulsesink_mute(self, mode: MuteMode):
        """Finds spotify's audio sink and toggles its mute state."""
        try:
            pacmd_out = subprocess.check_output(["pacmd", "list-sink-inputs"])
        except subprocess.CalledProcessError:
            log.error("Spotify sink not found. Is Pulse running? Resorting to pulse amixer as mute method.")
            self.mutemethod = self.pulse_mute  # Fall back to amixer mute.
            self.use_interlude_music = False
            return

        index, playback_state, muted_value = self.extract_pulse_sink_status(pacmd_out)

        self.song_status = "Playing" if playback_state == "RUNNING" else "Paused"
        self.is_sink_muted = False if muted_value == self.pulse_unmuted_value else True

        if index:
            if self.is_sink_muted and (mode == MuteMode.FORCE_UNMUTE or not self.current_song):
                log.info("Forcing unmute.")
                subprocess.call(["pacmd", "set-sink-input-mute", index, "0"])
            elif not self.is_sink_muted and mode == MuteMode.FORCE_MUTE:
                log.info("Muting {}.".format(self.current_song))
                subprocess.call(["pacmd", "set-sink-input-mute", index, "1"])
            elif self.is_sink_muted and mode == MuteMode.AUTO:
                log.info("Unmuting.")
                subprocess.call(["pacmd", "set-sink-input-mute", index, "0"])

    def prev(self):
        self.dbus.prev()
        self.player.try_resume_spotify_playback()

    def next(self):
        self.dbus.next()
        self.player.try_resume_spotify_playback()

    def signal_stop_received(self, sig, hdl):
        log.debug("{} received. Exiting safely.".format(sig))
        self.stop()

    def signal_block_received(self, sig, hdl):
        log.debug("Signal {} received. Blocking current song.".format(sig))
        self.block_current()

    def signal_unblock_received(self, sig, hdl):
        log.debug("Signal {} received. Unblocking current song.".format(sig))
        self.unblock_current()

    def signal_prev_received(self, sig, hdl):
        log.debug("Signal {} received. Playing previous interlude.".format(sig))
        self.prev()

    def signal_next_received(self, sig, hdl):
        log.debug("Signal {} received. Playing next song.".format(sig))
        self.next()

    def signal_playpause_received(self, sig, hdl):
        log.debug("Signal {} received. Toggling play state.".format(sig))
        self.dbus.playpause()

    def signal_toggle_block_received(self, sig, hdl):
        log.debug("Signal {} received. Toggling blocked state.".format(sig))
        self.toggle_block()

    def signal_prev_interlude_received(self, sig, hdl):
        log.debug("Signal {} received. Playing previous interlude.".format(sig))
        self.player.prev()

    def signal_next_interlude_received(self, sig, hdl):
        log.debug("Signal {} received. Playing next interlude.".format(sig))
        self.player.next()

    def signal_playpause_interlude_received(self, sig, hdl):
        log.debug("Signal {} received. Toggling interlude play state.".format(sig))
        self.player.playpause()

    def signal_toggle_autoresume_received(self, sig, hdl):
        log.debug("Signal {} received. Toggling autoresume.".format(sig))
        self.player.toggle_autoresume()

    def bind_signals(self):
        """Catch signals because it seems like a great idea, right? ... Right?"""
        signal.signal(signal.SIGINT, self.signal_stop_received)  # 9
        signal.signal(signal.SIGTERM, self.signal_stop_received)  # 15

        signal.signal(signal.SIGUSR1, self.signal_block_received)  # 10
        signal.signal(signal.SIGUSR2, self.signal_unblock_received)  # 12

        signal.signal(signal.SIGRTMIN, self.signal_prev_received)  # 34
        signal.signal(signal.SIGRTMIN + 1, self.signal_next_received)  # 35
        signal.signal(signal.SIGRTMIN + 2, self.signal_playpause_received)  # 35
        signal.signal(signal.SIGRTMIN + 3, self.signal_toggle_block_received)  # 37

        signal.signal(signal.SIGRTMIN + 10, self.signal_prev_interlude_received)  # 44
        signal.signal(signal.SIGRTMIN + 11, self.signal_next_interlude_received)  # 45
        signal.signal(signal.SIGRTMIN + 12, self.signal_playpause_interlude_received)  # 46
        signal.signal(signal.SIGRTMIN + 13, self.signal_toggle_autoresume_received)  # 47

    def prepare_stop(self):
        log.info("Exiting safely. Bye.")
        # Stop the interlude player.
        if self.use_interlude_music:
            self.use_interlude_music = False
            self.player.stop()
        # Save the list only if it changed during runtime.
        if self.blocklist != self.orglist:
            self.blocklist.save()
        # Unmute before exiting.
        self.toggle_mute(MuteMode.FORCE_UNMUTE)

    def stop(self):
        self.prepare_stop()
        Gtk.main_quit()
        sys.exit()

    def toggle_block(self):
        """Block/unblock the current song."""
        if self.found:
            self.unblock_current()
        else:
            self.block_current()
            if self.use_interlude_music:
                self.player.manual_control = False

    @property
    def automute(self):
        return self._automute

    @automute.setter
    def automute(self, boolean):
        log.debug("Automute: {}.".format(boolean))
        self._automute = boolean

    @property
    def autodetect(self):
        return self._autodetect

    @autodetect.setter
    def autodetect(self, boolean):
        log.debug("Autodetect: {}.".format(boolean))
        self._autodetect = boolean


def initialize(doc=__doc__):
    try:
        args = util.docopt(doc, version="blockify {}".format(util.VERSION))
    except Exception:
        args = None
    util.initialize(args)

    _blocklist = blocklist.Blocklist()
    cli = Blockify(_blocklist)

    return cli


def main():
    """Entry point for the CLI-version of Blockify."""
    cli = initialize()
    cli.start()


if __name__ == "__main__":
    main()
