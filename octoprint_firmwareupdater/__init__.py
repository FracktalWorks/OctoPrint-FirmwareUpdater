# coding=utf-8
from __future__ import absolute_import

import flask
import json
import os
import requests
import tempfile
import time
import urllib
import urllib2
import urlparse

import octoprint.plugin

from octoprint.server.util.flask import restricted_access
from octoprint.server import admin_permission
from octoprint.events import Events

class FirmwareupdaterPlugin(octoprint.plugin.BlueprintPlugin,
	octoprint.plugin.TemplatePlugin,
	octoprint.plugin.AssetPlugin,
	octoprint.plugin.SettingsPlugin,
	octoprint.plugin.EventHandlerPlugin):

	def __init__(self):
		self.printer_info = None
		self.update_info = None
		self.force_check_updates = False
		self._checking = False

	#~~ BluePrint API

	@octoprint.plugin.BlueprintPlugin.route("/flashFirmwareWithPath", methods=["POST"])
	@restricted_access
	@admin_permission.require(403)
	def flash_firmware_with_path(self):
		if not self._check_avrdude():
			return flask.make_response("Error.", 500)

		input_name = "file"
		input_upload_name = input_name + "." + self._settings.global_get(["server", "uploads", "nameSuffix"])
		input_upload_path = input_name + "." + self._settings.global_get(["server", "uploads", "pathSuffix"])

		uploaded_hex_path = flask.request.values[input_upload_path]
		selected_port = flask.request.values["selected_port"]

		import shutil
		try:
			temp_hex_file = tempfile.NamedTemporaryFile(mode='r+b')
			with open(os.path.abspath(uploaded_hex_path),'r+b') as f:
				shutil.copyfileobj(f, temp_hex_file)
		except:
			self._logger.exception(u"Error when copying uploaded temp hex file")
		else:
			temp_hex_file.seek(0)

		# Create thread to flash firmware
		import threading
		flash_thread = threading.Thread(target=self._flash_worker, args=(temp_hex_file, selected_port))
		flash_thread.daemon = False
		flash_thread.start()

		return flask.make_response("Ok.", 200)

	@octoprint.plugin.BlueprintPlugin.route("/flashFirmwareWithURL", methods=["POST"])
	@restricted_access
	@admin_permission.require(403)
	def flash_firmware_with_url(self):
		if not self._check_avrdude():
			return flask.make_response("Error.", 500)

		hex_url = flask.request.json['hex_url']
		self._selected_port = flask.request.json['selected_port']

		ret = self._flash_firmware_with_url(hex_url)

		if ret:
			return flask.make_response("Ok.", 200)
		else:
			return flask.make_response("Error.", 500)

	@octoprint.plugin.BlueprintPlugin.route("/flashUpdate", methods=["POST"])
	@restricted_access
	@admin_permission.require(403)
	def flash_update(self):
		if not self._check_avrdude():
			return flask.make_response("Error.", 500)

		self._selected_port = flask.request.json['selected_port']

		if self.update_info is not None:
			ret = self._flash_firmware_with_url(self.update_info["ota"]["url"])
			if ret:
				return flask.make_response("Ok.", 200)
			else:
				return flask.make_response("Error.", 500)
		else:
			self._logger.error(u"No update info found")
			self._send_status(status_type="flashing_status", status_value="error", status_description="No update info found")
			return flask.make_response("Error.", 500)

	def _flash_firmware_with_url(self, hex_url):

		temp_hex_file = tempfile.NamedTemporaryFile(mode='r+b')

		try:
			urllib.urlretrieve(hex_url, temp_hex_file.name)
		except:
			self._logger.exception(u"Error when retrieving hex file from URL")
			return False

		# Create thread to flash firmware
		import threading
		flash_thread = threading.Thread(target=self._flash_worker, args=(temp_hex_file, self._selected_port))
		flash_thread.daemon = False
		flash_thread.start()

		return True

	def _flash_worker(self, hex_file, selected_port):

		avrdude_path = self._settings.get(["avrdude_path"])
		working_dir = os.path.dirname(avrdude_path)
		hex_path = hex_file.name
		avrdude_command = [avrdude_path, "-v", "-p", "m2560", "-c", "wiring", "-P", selected_port, "-U", "flash:w:" + hex_path + ":i", "-D"]

		import sarge
		self._logger.info(u"Running %r in %s" % (' '.join(avrdude_command), working_dir))
		try:
			p = sarge.run(avrdude_command, cwd=working_dir, async=True, stdout=sarge.Capture(), stderr=sarge.Capture())
			p.wait_events()

			while p.returncode is None:
				line = p.stderr.read(timeout=0.5)
				if not line:
					p.commands[0].poll()
					continue
				if "avrdude: writing" in line:
					self._logger.info(u"Writing memory...")
					self._send_status(status_type="flashing_status", status_value="progress", status_description="Writting memory...")
				elif "avrdude: verifying ..." in line:
					self._logger.info(u"Verifying memory...")
					self._send_status(status_type="flashing_status", status_value="progress", status_description="Verifying memory...")
				elif "timeout communicating with programmer" in line:
					e_msg = "Timeout communicating with programmer"
					raise AvrdudeException
				elif "avrdude: ERROR:" in line:
					e_msg = "Avrdude error: " + line[line.find("avrdude: ERROR:")+len("avrdude: ERROR"):].strip()
					raise AvrdudeException

			if p.returncode == 0:
				self._logger.info(u"Flashing successful.")
				self._send_status(status_type="flashing_status", status_value="successful")
				self._send_status(status_type="check_update_status", status_value="up_to_date")
			else:
				e_msg = "Avrdude returned code {returncode}".format(returncode=p.returncode)
				raise AvrdudeException

		except AvrdudeException:
			self._logger.error(u"Flashing failed. {error}.".format(error=e_msg))
			self._send_status(status_type="flashing_status", status_value="error", status_description=e_msg)
		except:
			self._logger.exception(u"Flashing failed. Unexpected error.")
			self._send_status(status_type="flashing_status", status_value="error", status_description="Unexpected error")
		finally:
			hex_file.close()

	def _check_avrdude(self):
		avrdude_path = self._settings.get(["avrdude_path"])
		if not os.path.exists(avrdude_path):
			self._logger.error(u"Path to avrdude does not exist: {path}".format(path=avrdude_path))
			return False
		elif not os.path.isfile(avrdude_path):
			self._logger.error(u"Path to avrdude is not a file: {path}".format(path=avrdude_path))
			return False
		elif not os.access(avrdude_path, os.X_OK):
			self._logger.error(u"Path to avrdude is not executable: {path}".format(path=avrdude_path))
			return False
		else:
			return True

	@octoprint.plugin.BlueprintPlugin.route("/checkForUpdates", methods=["POST"])
	def check_for_updates(self):
		self._selected_port = flask.request.json['selected_port']
		self.force_check_updates = True
		self._send_status(status_type="check_update_status", status_value="progress", status_description="Connecting to Printer...")
		self._printer.connect(port=self._selected_port)
		return flask.make_response("Ok.", 200)

	#~~ EventHandler API

	def on_event(self, event, payload):
		if event == Events.CONNECTED:
			if not self.force_check_updates and not self._settings.get(["check_after_connect"]):
				return

			self.force_check_updates = False
			self.printer_info = None
			self.update_info = None

			self.callback = octoprint.printer.PrinterCallback()
			self.default_on_printer_add_message = self.callback.on_printer_add_message
			self.callback.on_printer_add_message = self.on_printer_add_message
			self._printer.register_callback(self.callback)

			self._send_status(status_type="check_update_status", status_value="progress", status_description="Retrieving current firmware version from printer...")
			self._logger.info(u"Retrieving current firmware version from printer...")
			self._printer.commands("M115\n")

			self.start_time = time.time()
			self._checking = True
			return

		elif event == Events.DISCONNECTED:
			if self._checking:
				self.callback.on_printer_add_message = self.default_on_printer_add_message # Unregister callback
				self._send_status(status_type="check_update_status", status_value="error", status_description="Printer was disconnected")
				self._checking = False
			self.printer_info = None
			return

	def on_printer_add_message(self, data):
		if time.time() - self.start_time > 30 or time.time() < self.start_time:
			self.printer_info = None
			self.callback.on_printer_add_message = self.default_on_printer_add_message # Unregister callback
			self._send_status(status_type="check_update_status", status_value="error", status_description="Unable to get FW version from printer")
			self._logger.error(u"Unable to get FW version from printer")
			self._checking = False
			
		if not ("MACHINE_TYPE" in data and "FIRMWARE_VERSION" in data):
			return

		self.callback.on_printer_add_message = self.default_on_printer_add_message # Unregister callback
		self._checking = False

		import re
		try:
			m115_regex = re.compile("(?P<key>[A-Z_-]+):(?P<value>.*?)((\s+(?=[A-Z_-]+:))|$)")
			self.printer_info = dict((m.group("key"), m.group("value")) for m in m115_regex.finditer(data))
		except:
			self._send_status(status_type="check_update_status", status_value="error", status_description="Unable to parse M115 response")
			self._logger.exception(u"Unable to parse M115 response.")

		self._logger.info(u"Connected printer: {printer_model} (FW version: {fw_version})".format(printer_model=self.printer_info["MACHINE_TYPE"], fw_version=self.printer_info["FIRMWARE_VERSION"]))

		printer_model = urllib.quote(self.printer_info["MACHINE_TYPE"])
		fw_version = urllib.quote(self.printer_info["FIRMWARE_VERSION"])
		fw_language = urllib.quote(self.printer_info["X-FIRMWARE_LANGUAGE"])
		ws_url = self._settings.get(["_update_service_url"]).format(model=printer_model, fw_version=fw_version, language=fw_language)

		try:
			ws_response = requests.get(ws_url)
		except:
			self._send_status(status_type="check_update_status", status_value="error", status_description="Unable to connect to update server")
			self._logger.exception(u"Unable to connect to update server.")
			return

		if ws_response.status_code != 200:
			self._send_status(status_type="check_update_status", status_value="error", status_description="Unable to connect to update server")
			self._logger.error(u"Unable to connect to update server: Got status code {sc}".format(sc=ws_response.status_code))
			return

		self.update_info = ws_response.json()
		if self.update_info["available"]:
			self._send_status(status_type="check_update_status", status_value="update_available", status_description=self.update_info["ota"]["fw_version"])
			self._logger.info(u"Firmware update available (FW version: %s)" % self.update_info["ota"]["fw_version"])
			return
		else:
			self._send_status(status_type="check_update_status", status_value="up_to_date")
			self._logger.info(u"Firmware is up to date")
			return

	#~~ SettingsPlugin API

	def get_settings_defaults(self):
		return {
			"avrdude_path": None,
			"check_after_connect": True,
			"_update_service_url": "http://localhost:8080/api/checkUpdate/{model}/{fw_version}/{language}"
		}

	#~~ Asset API

	def get_assets(self):
		return dict(js=["js/firmwareupdater.js"])

	#~~ Extra methods

	def _send_status(self, status_type, status_value, status_description=""):
		self._plugin_manager.send_plugin_message(self._identifier, dict(type="status", status_type=status_type, status_value=status_value, status_description=status_description))

	#~~ Hooks

	def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
		return [("POST", r"/flashFirmwareWithPath", 1000 * 1024)]


class AvrdudeException(Exception):
	pass

__plugin_name__ = "Firmware Updater"

def __plugin_load__():
	global __plugin_implementation__
	global __plugin_hooks__

	__plugin_implementation__ = FirmwareupdaterPlugin()

	__plugin_hooks__ = {
        "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook
    }