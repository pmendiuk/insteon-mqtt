#===========================================================================
#
# Insteon modem class.
#
#===========================================================================
import json
import os
from .Address import Address
from .CommandSeq import CommandSeq
from . import config
from . import db
from . import handler
from . import log
from . import message as Msg
from . import util
from .Signal import Signal

LOG = log.get_logger()


class Modem:
    """Insteon modem class

    The modem class handles commands to send to the PLM modem.  It
    also stores the device definitions by address (read from a
    configuration input).  This allows devices to be looked up by
    address to send commands to those devices.
    """
    def __init__(self, protocol):
        """Constructor

        Actual modem definitions must be loaded from a configuration
        file via load_config() before the modem can be used.

        Args:
          protocol:  (Protocol) Insteon message handling protocol object.
        """
        self.protocol = protocol

        self.addr = None
        self.name = "modem"
        self.label = self.name

        self.save_path = None

        # Map of Address.id -> Device and name -> Device.  name is
        # optional so devices might not be in that map.
        self.devices = {}
        self.device_names = {}
        self.scenes = {}
        self.db = db.Modem()

        # Signal to emit when a new device is added.
        self.signal_new_device = Signal()  # emit(modem, device)

        # Remove (mqtt) commands mapped to methods calls.  These are
        # handled in run_command().  Commands should all be lower case
        # (inputs are lowered).
        self.cmd_map = {
            'db_add_ctrl_of' : self.db_add_ctrl_of,
            'db_add_resp_of' : self.db_add_resp_of,
            'db_del_ctrl_of' : self.db_del_ctrl_of,
            'db_del_resp_of' : self.db_del_resp_of,
            'refresh' : self.refresh,
            'refresh_all' : self.refresh_all,
            'linking' : self.linking,
            }

        # Add a generic read handler for any broadcast messages
        # initiated by the Insteon devices.
        self.protocol.add_handler(handler.Broadcast(self))

        # Handle all link complete messages that the modem sends when the set
        # button or linking mode is finished.
        self.protocol.add_handler(handler.ModemLinkComplete(self))

        # Handle user triggered factory reset of the modem.
        self.protocol.add_handler(handler.ModemReset(self))

    #-----------------------------------------------------------------------
    def type(self):
        """Return a nice class name for the device.
        """
        return "Modem"

    #-----------------------------------------------------------------------
    def load_config(self, data):
        """Load a configuration dictionary.

        This should be the insteon key in the configuration data.  Key
        inputs are:

        - port      The serial device to talk to.  This is a path to the
                    modem (or a network url).  See pyserial for inputs.
        - baudrate  Optional baud rate of the serial line.
        - address   Insteon address of the modem.  See Address for inputs.
        - storage   Path to store database records in.
        - startup_refresh    True if device databases should be checked for
                             new entries on start up.
        - devices   List of devices.  Each device is a type and insteon
                    address of the device.

        Args:
          data:   (dict) Configuration data to load.
        """
        LOG.info("Loading configuration data")

        # Pass the data to the modem network link.
        self.protocol.load_config(data)

        # Read the modem address.
        self.addr = Address(data['address'])
        self.label = "%s (%s)" % (self.addr, self.name)
        LOG.info("Modem address set to %s", self.addr)

        # Load the modem database.
        if 'storage' in data:
            save_path = data['storage']
            if not os.path.exists(save_path):
                os.makedirs(save_path)

            self.save_path = save_path
            self.load_db()

            LOG.info("Modem %s database loaded %s entries", self.addr,
                     len(self.db))
            LOG.debug(str(self.db))

        # Read the device definitions and scenes.
        self._load_devices(data.get('devices', []))
        #FUTURE: self.scenes = self._load_scenes(data.get('scenes', []))

        # Send refresh messages to each device to check if the
        # database is up to date.
        if data.get('startup_refresh', False) is True:
            LOG.info("Starting device refresh")
            for device in self.devices.values():
                device.refresh()

    #-----------------------------------------------------------------------
    def refresh(self, force=False, on_done=None):
        """Load the all link database from the modem.

        This sends a message to the modem to start downloading the all
        link database.  The message handler handler.ModemDbGet is used to
        process the replies and update the modem database.

        Args:
           force:   (bool) Ignored - this insures a consistent API with the
                    device refresh command.
        TODO: doc
        """
        LOG.info("Modem sending get first db record command")

        # Clear the db so we can rebuild it.
        self.db.clear()

        # Request the first db record from the handler.  The handler
        # will request each next record as the records arrive.
        msg = Msg.OutAllLinkGetFirst()
        msg_handler = handler.ModemDbGet(self.db, on_done)
        self.protocol.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def db_path(self):
        """Return the all link database path.

        This will be the configuration save_path directory and the
        file name will be the modem hex address with a .json suffix.
        """
        return os.path.join(self.save_path, self.addr.hex) + ".json"

    #-----------------------------------------------------------------------
    def load_db(self):
        """Load the all link database from a file.

        The file is stored in JSON format (by save_db()) and has the
        path self.db_path().  If the file doesn't exist, nothing is
        done.
        """
        # See if the database file exists.  Tell the modem it's future
        # path so it can save itself.
        path = self.db_path()
        self.db.set_path(path)
        if not os.path.exists(path):
            return

        # Read the file and convert it to a db.Modem object.
        try:
            with open(path) as f:
                data = json.load(f)

            self.db = db.Modem.from_json(data, path)
        except:
            LOG.exception("Error reading modem db file %s", path)
            return

        LOG.info("%s database loaded %s entries", self.addr, len(self.db))
        LOG.debug("%s", self.db)

    #-----------------------------------------------------------------------
    def add(self, device):
        """Add a device object to the modem.

        This doesn't change the modem all link database, it just
        allows us to find the input device by address.

        Args:
          device    The device object to add.

        """
        self.devices[device.addr.id] = device
        if device.name:
            self.device_names[device.name] = device

    #-----------------------------------------------------------------------
    def remove(self, device):
        """Remove a device object from the modem.

        This doesn't change the modem all link database, it just
        removes the input device from our local look up.

        Args:
          device    The device object to add.  If the device doesn't exist,
                    nothing is done.
        """
        self.devices.pop(device.addr.id, None)
        if device.name:
            self.device_names.pop(device.name, None)

    #-----------------------------------------------------------------------
    def find(self, addr):
        """Find a device by address.

        NOTE: this searches devices in the config file.  We don't ping
        the modem to find the devices because disovery isn't the most
        reliable.

        Args:
          addr:   (Address) The Insteon address object to find.  This can
                  also be a string or integer (see the Address constructor for
                  other options.  This can also be the modem address in which
                  case this object is returned.

        Returns:
          Returns the device object or None if it doesn't exist.
        """
        # Handle string device name requests.
        if isinstance(addr, str):
            addr = addr.lower()

        if addr == "modem":
            return self

        # See if the input is one of the "nice" device names.
        device = self.device_names.get(addr, None)
        if device:
            return device

        # Otherwise, try and parse the input as an Insteon address.
        try:
            addr = Address(addr)
        except:
            LOG.exception("Invalid Insteon address or unknown device name "
                          "'%s'", addr)
            return None

        # Device address is the modem.
        if addr == self.addr:
            return self

        # Otherwise try and find the device by address.  None is
        # returned if it doesn't exist.
        device = self.devices.get(addr.id, None)
        return device

    #-----------------------------------------------------------------------
    def refresh_all(self, force=False, on_done=None):
        """Refresh all the all link databases.

        This forces a refresh of the modem and device databases.  This
        can take a long time - up to 5 seconds per device some times
        depending on the database sizes.  So it usually should only be
        called if no other activity is expected on the network.
        """
        # Reload the modem database.
        self.refresh()

        # Reload all the device databases.
        for i, device in enumerate(self.devices.values()):
            # Only set the callback if this is the last element.
            callback = None
            if i == len(self.devices) - 1:
                callback = on_done

            device.refresh(force, on_done=callback)

    #-----------------------------------------------------------------------
    def db_add_ctrl_of(self, addr, group, data=None, two_way=True,
                       refresh=True, on_done=None):
        """Add the modem as a controller of a device.

        This updates the modem's all link database to show that the
        model is controlling an Insteon device.  If two_way is True,
        the corresponding responder link on the device is also
        created.  This two-way link is required for the device to
        accept commands from the modem.

        Normally, pressing the set button the modem and then the
        device will configure this link using group 1.

        The 3 byte data entry is usually (on_level, ramp_rate, unused)
        where those values are 1 byte (0-255) values but those fields
        are device dependent.

        The optional callback has the signature:
            on_done(bool success, str message, entry)

        - success is True if both commands worked or False if any failed.
        - message is a string with a summary of what happened.  This is used
          for user interface responses to sending this command.
        - entry is either the db.ModemEntry or db.DeviceEntry that was
          updated.

        Args:
          addr:     (Address) The remote device address.
          group:    (int) The group to add link for.
          data:     (bytes[3]) 3 byte data entry.
          two_way:  (bool) If True, after creating the controller link on the
                    modem, a responder link is created on the remote device
                    to form the required pair of entries.
          refresh:  (bool) If True, call refresh before changing the db.
                    This is ignored on the modem since it doesn't use memory
                    addresses and can't be corrupted.
          on_done:  Optional callback run when both commands are finished.
        """
        self._db_update(addr, group, data, two_way, is_controller=True,
                        refresh=refresh, on_done=on_done)

    #-----------------------------------------------------------------------
    def db_add_resp_of(self, addr, group, data=None, two_way=True,
                       refresh=True, on_done=None):
        """Add the modem as a responder of a device.

        This updates the modem's all link database to show that the
        model is responding to an Insteon device.  If two_way is True,
        the corresponding controller link on the device is also
        created.  This two-way link is required for the device to send
        commands to the modem and for the modem to report device state
        changes.

        Normally, pressing the set button the device and then the
        modem will configure this link using group 1.

        The 3 byte data entry is usually (on_level, ramp_rate, unused)
        where those values are 1 byte (0-255) values but those fields
        are device dependent.

        The optional callback has the signature:
            on_done(bool success, str message, entry)

        - success is True if both commands worked or False if any failed.
        - message is a string with a summary of what happened.  This is used
          for user interface responses to sending this command.
        - entry is either the db.ModemEntry or db.DeviceEntry that was
          updated.

        Args:
          addr:     (Address) The remote device address.
          group:    (int) The group to add link for.
          data:     (bytes[3]) 3 byte data entry.
          two_way:  (bool) If True, after creating the responder link on the
                    modem, a controller link is created on the remote device
                    to form the required pair of entries.
          refresh:  (bool) If True, call refresh before changing the db.
                    This is ignored on the modem since it doesn't use memory
                    addresses and can't be corrupted.
          on_done:  Optional callback run when both commands are finished.
        """
        self._db_update(addr, group, data, two_way, is_controller=False,
                        refresh=refresh, on_done=on_done)

    #-----------------------------------------------------------------------
    def db_del_ctrl_of(self, addr, group, two_way=True, refresh=True,
                       on_done=None):
        """TODO: doc
        """
        # Call with is_controller=True
        self._db_delete(addr, group, True, two_way, refresh, on_done)

    #-----------------------------------------------------------------------
    def db_del_resp_of(self, addr, group, two_way=True, refresh=True,
                       on_done=None):
        """TODO: doc
        """
        # Call with is_controller=False
        self._db_delete(addr, group, False, two_way, refresh, on_done)

    #-----------------------------------------------------------------------
    def factory_reset(self):
        """TODO: doc
        """
        LOG.warning("Modem being reset.  All data will be lost")
        msg = Msg.OutResetModem()
        msg_handler = handler.ModemReset(self)
        self.protocol.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def linking(self, group=0x01, on_done=None):
        """TODO: doc
        """
        # Tell the modem to enter all link mode for the group.  The
        # handler will handle timeouts (to send the cancel message) if
        # nothing happens.  See the handler for details.
        msg = Msg.OutModemLinking(Msg.OutModemLinking.Cmd.EITHER, group)
        msg_handler = handler.ModemLinkStart(on_done)
        self.protocol.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def link_data(self, group, is_controller):
        """TODO: doc
        """
        # Normally, the modem (ctrl) -> device (resp) link is created using
        # the linking() command - then the handler.ModemLinkComplete will
        # fill these values in for us using the device information.  But they
        # probably aren't used so it doesn't really matter.
        if is_controller:
            return bytes([group, 0x00, 0x00])

        # Responder data is a mystery on the modem.  This seems to work but
        # it's unclear if it's needed at all.
        else:
            return bytes([group, 0x00, 0x00])

    #-----------------------------------------------------------------------
    def run_scene(self, group, is_on, cmd1=None, cmd2=0x00):
        """TODO: doc
        """
        # TODO: add modem scene support.
        if cmd1 is None:
            cmd1 = 0x11 if is_on else 0x13

        #msg = Msg.OutModemScene(group, cmd1, cmd2)
        #msg_handler = handler.???
        # see test script for reply - this does work.  Modem must be
        # controller of device.  Modem(ctrl group)->Device(resp group) so we
        # need multi group linking support before this can be done properly.

    #-----------------------------------------------------------------------
    def run_command(self, **kwargs):
        """Run arbitrary commands.

        Commands are input as a dictionary:
          { 'cmd' : 'COMMAND', ...args }

        where COMMAND is the command name and any additional arguments
        to the command are other dictionary keywords.  Valid commands
        are:
          getdb:  No arguments.  Download the PLM modem all link database
                  and save it to file.

          reload_all: No arguments.  Reloads the modem database and tells
                      every device to reload it's database as well.

          factory_reset: No arguments.  Full factory reset of the modem.

          set_btn: Optional time_out argument (in seconds).  Simulates pressing
                   the modem set button to put the modem in linking mode.
        """
        cmd = kwargs.pop('cmd', None)
        if not cmd:
            LOG.error("Invalid command sent to modem %s.  No 'cmd' "
                      "keyword: %s", self.addr, kwargs)
            return

        cmd = cmd.lower().strip()
        func = self.cmd_map.get(cmd, None)
        if not func:
            LOG.error("Invalid command sent to modem %s.  Input cmd "
                      "'%s' not valid.  Valid commands: %s", self.addr,
                      cmd, self.cmd_map.keys())
            return

        # Call the command function with any remaining arguments.
        try:
            func(**kwargs)
        except:
            LOG.exception("Invalid command inputs to modem %s.  Input "
                          "cmd %s with args: %s", self.addr, cmd, str(kwargs))

    #-----------------------------------------------------------------------
    def handle_group_cmd(self, addr, msg):
        """Handle a group command addressed to the modem.

        This is called when a broadcast message is sent from a device
        that is triggered (like a motion sensor or clicking a light
        switch).  The device that sent the message will look up it's
        associations in it's all link database and call the
        handle_group_cmd() on each device that are in it's scene.

        Args:
           addr:   (Address) The address the message is from.
           msg:    (message.InpStandard) Broadcast group message.
        """
        # The modem has nothing to do for these messages.
        pass

    #-----------------------------------------------------------------------
    def _load_devices(self, data):
        """Load device definitions from a configuration data object.

        The input is the insteon.devices configuration dictionary.
        Keys are the device type.  Value is the list of devices.  See
        config.yaml or the package documentation for an example.

        Args:
          data:   Configuration devices dictionary.
        """
        self.devices.clear()
        self.device_names.clear()

        for device_type in data:
            # Use a default list so that if the config field is empty,
            # the loop below will still work.
            values = data[device_type]
            if not values:
                values = []

            # Look up the device type in the configuration data and
            # call the constructor to build the device object.
            dev_class, kwargs = config.find(device_type)

            # Have the device type parse the config values below here and
            # return us a list of devices.
            devices = dev_class.from_config(values, self.protocol, self,
                                            **kwargs)

            for dev in devices:
                LOG.info("Created %s at %s", device_type, dev.label)

                # Store the device by ID in the map.
                self.add(dev)

                # Notify anyone else that new device is available.
                self.signal_new_device.emit(self, dev)

    #-----------------------------------------------------------------------
    def _load_scenes(self, data):
        """Load virtual modem scenes from a configuration dict.

        Load scenes from the configuration file.  Virtual scenes are
        defined in software - they are links where the modem is the
        controller and devices are the responders.  These are scenes
        we can trigger by a command to the modem which will broadcast
        a message to update all the edeives.

        Args:
          data:   Configuration dictionary for scenes.
        """
        # TODO: support scene loading
        # Read scenes from the configuration file.  See if the scene
        # has changed vs what we have in the device databases.  If it
        # has, we need to update the device databases.
        scenes = {}
        return scenes

    #-----------------------------------------------------------------------
    def _db_update(self, addr, group, data, two_way, is_controller, refresh,
                   on_done):
        """Update the modem database.

        See db_add_ctrl_of() or db_add_resp_of() for docs.
        """
        # Find the remote device.  Update addr since the input may be a name.
        remote = self.find(addr)
        if remote:
            addr = remote.addr

        # If don't have an entry for this device, we can't sent it commands.
        if two_way and not remote:
            LOG.info("Modem db add %s can't find remote device %s.  "
                     "Link will be only one direction",
                     util.ctrl_str(is_controller), addr)

        seq = CommandSeq(self.protocol, "Device db update complete", on_done)

        # Get the data array to use.  See Github issue #7 for discussion.
        # Use teh bytes() cast here so we can take a list as input.
        if data is None:
            data = self.link_data(group, is_controller)
        else:
            data = bytes(data)

        # Create a new database entry for the modem and send it to the
        # modem for updating.
        entry = db.ModemEntry(addr, group, is_controller, data)
        seq.add(self.db.add_on_device, self.protocol, entry)

        # For two way commands, insert a callback so that when the
        # modem command finishes, it will send the next command to the
        # device.  When that finishes, it will run the input callback.
        if two_way and remote:
            two_way = False
            remote_data = None
            if is_controller:
                seq.add(remote.db_add_resp_of, self.addr, group, remote_data,
                        two_way, refresh)
            else:
                seq.add(remote.db_add_ctrl_of, self.addr, group, remote_data,
                        two_way, refresh)

        # Start the command sequence.
        seq.run()

    #-----------------------------------------------------------------------
    def _db_delete(self, addr, group, is_controller, two_way, refresh,
                   on_done):
        """TODO: doc
        """
        LOG.debug("db delete: %s grp=%s ctrl=%s 2w=%s", addr, group,
                  util.ctrl_str(is_controller), two_way)

        # Find the remote device.  Update addr since the input may be a name.
        remote = self.find(addr)
        if remote:
            addr = remote.addr

        # If don't have an entry for this device, we can't sent it commands
        if two_way and not remote:
            LOG.ui("Device db delete %s can't find remote device %s.  "
                   "Link will be only deleted one direction",
                   util.ctrl_str(is_controller), addr)

        # Find teh database entry being deleted.
        entry = self.db.find(addr, group, is_controller)
        if not entry:
            LOG.warning("Device %s delete no match for %s grp %s %s",
                        self.addr, addr, group, util.ctrl_str(is_controller))
            on_done(False, "Entry doesn't exist", None)
            return

        # Add the function delete call to the sequence.
        seq = CommandSeq(self.protocol, "Delete complete", on_done)
        seq.add(self.db.delete_on_device, self.protocol, entry)

        # For two way commands, insert a callback so that when the modem
        # command finishes, it will send the next command to the device.
        # When that finishes, it will run the input callback.
        if two_way and remote:
            two_way = False
            if is_controller:
                seq.add(remote.db_del_resp_of, self.addr, group, two_way,
                        refresh)
            else:
                seq.add(remote.db_del_ctrl_of, self.addr, group, two_way,
                        refresh)

        # Start running the commands.
        seq.run()

    #-----------------------------------------------------------------------
