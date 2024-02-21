#!/usr/bin/python3
#
# This script set fan speed and monitor power button events.
#
# Fan Speed is set by sending 0 to 100 to the MCU (Micro Controller Unit)
# The values will be interpreted as the percentage of fan speed, 100% being maximum
#
# Power button events are sent as a pulse signal to BCM Pin 4 (BOARD P7)
# A pulse width of 20-30ms indicates reboot request (double-tap)
# A pulse width of 40-50ms indicates shutdown request (hold and release after 3 secs)
#
# Additional comments are found in each function below
#
# Standard Deployment/Triggers:
#  * Raspbian, OSMC: Runs as service via /lib/systemd/system/argononed.service
#  * lakka, libreelec: Runs as service via /storage/.config/system.d/argononed.service
#  * recalbox: Runs as service via /etc/init.d/
#


import xbmc
import xbmcaddon

# workaround for lgpio issue
# https://github.com/gpiozero/gpiozero/issues/1106
import os, tempfile
pid = os.getpid()
tmp_lgpio_work_dir = tempfile.TemporaryDirectory()
os.environ["LG_WD"] = tmp_lgpio_work_dir.name

# For Libreelec/Lakka, note that we need to add system paths
import sys
sys.path.append('/storage/.kodi/addons/virtual.rpi-tools/lib')
from gpiozero import Device, Button
from gpiozero import pi_info

import time
from shutil import copyfile

import zlib

from resources.lib.argonregister import *

# Detect the RPi5 to initialize the RP1 chip
pi = pi_info()
if pi.model == '5B':
	import importlib.util
	lgpio_spec = importlib.util.find_spec("lgpio")
	if lgpio_spec is not None:
		from gpiozero.pins.lgpio import LGPIOFactory
		Device.pin_factory = LGPIOFactory(chip=4)

# Initialize I2C Bus
bus = argonregister_initializebusobj()

fansettingupdate=False
power_btn_triggered=False

# Detect Settings Change

class SettingMonitor(xbmc.Monitor):
	def onSettingsChanged(self):
		global fansettingupdate
		fansettingupdate = True

# quick interruptible sleep

def thread_sleep(sleep_sec, event):
	for i in range(sleep_sec):
		if event.is_set():
			break
		time.sleep(1)

# This function is the thread that monitors activity in our shutdown pin
# The pulse width is measured, and the corresponding shell command will be issued

def power_btn_pressed():
	global power_btn_triggered
	power_btn_triggered = True

def shutdown_check(event):
	global power_btn_triggered
	power_btn_triggered = False
	shutdown_pin=4
	# pull down the pin
	btn = Button(shutdown_pin, pull_up = False)
	btn.when_pressed = power_btn_pressed

	while True:
		pulsetime = 1
		time.sleep(0.001)
		if power_btn_triggered:
			time.sleep(0.01)
			while btn.is_pressed:
				time.sleep(0.01)
				pulsetime += 1
				if event.is_set():
					break
			if pulsetime >=2 and pulsetime <=3:
				xbmc.restart()
			elif pulsetime >=4 and pulsetime <=5:
				xbmc.shutdown()
		if event.is_set():
			break

# This function converts the corresponding fanspeed for the given temperature
# The configuration data is a list of strings in the form "<temperature>=<speed>"

def get_fanspeed(tempval, configlist):
	for curconfig in configlist:
		curpair = curconfig.split("=")
		tempcfg = float(curpair[0])
		fancfg = int(float(curpair[1]))
		if tempval >= tempcfg:
			if fancfg < 1:
				return 0
			elif fancfg < 10:
				return 10
			return fancfg
	return 0

# This function retrieves the fanspeed configuration list from a file, arranged by temperature
# It ignores lines beginning with "#" and checks if the line is a valid temperature-speed pair
# The temperature values are formatted to uniform length, so the lines can be sorted properly

def load_config():
	ADDON = xbmcaddon.Addon()

	fanspeed_disable = ADDON.getSettingBool('fanspeed_disable')
	if fanspeed_disable == True:
		return ["90=100"]
	fanspeed_alwayson = ADDON.getSettingBool('fanspeed_alwayson')
	if fanspeed_alwayson == True:
		return ["1=100"]

	newconfig = []

	configtype = ['a', 'b', 'c']
	for typekey in configtype:
		tempval = float(ADDON.getSetting('devtemp_'+typekey))
		fanval = int(ADDON.getSetting('fanspeed_'+typekey))

		newconfig.append( "{:5.1f}={}".format(tempval,fanval))

	if len(newconfig) > 0:
		newconfig.sort(reverse=True)


	return newconfig

# This function is the thread that monitors temperature and sets the fan speed
# The value is fed to get_fanspeed to get the new fan speed
# To prevent unnecessary fluctuations, lowering fan speed is delayed by 30 seconds
#
# Location of config file varies based on OS
#
def temp_check(event):
	global fansettingupdate

	argonregsupport = argonregister_checksupport(bus)

	fanconfig = ["65=100", "60=55", "55=10"]
	prevblock=0

	while True:
		tmpconfig = load_config()
		if len(tmpconfig) > 0:
			fanconfig = tmpconfig
		fansettingupdate = False
		while fansettingupdate == False:
			try:
				tempfp = open("/sys/class/thermal/thermal_zone0/temp", "r")
				temp = tempfp.readline()
				tempfp.close()
				val = float(int(temp)/1000)
			except IOError:
				val = 0

			block = get_fanspeed(val, fanconfig)
			if block < prevblock:
				thread_sleep(30, event)
			prevblock = block
			try:
				argonregister_setfanspeed(bus, block, argonregsupport)
				thread_sleep(30, event)
			except IOError:
				temp=""
				thread_sleep(60, event)
			if event.is_set():
				break
		if event.is_set():
			break

#
# Used to enabled i2c and UART
#

def checksetup():
	configfile = "/flash/config.txt"

	# Add argon remote control
	lockfile = "/storage/.config/argon40_rc.lock"
	if os.path.exists(lockfile) == False:
		copykeymapfile()
		copyrcmapsfile()
		removelircfile()

	# Check if i2c exists
	isenabled = False
	with open(configfile, "r") as fp:
		for curline in fp:
			if not curline:
				continue
			tmpline = curline.strip()
			if not tmpline:
				continue
			if tmpline == "dtparam=i2c=on":
				isenabled = True
				break
	if isenabled == True:
		return()

	os.system("mount -o remount,rw /flash")
	with open(configfile, "a") as fp:
		fp.write("dtparam=i2c=on\n")
		fp.write("enable_uart=1\n")
		fp.write("dtoverlay=gpio-ir,gpio_pin=23\n")
	os.system("mount -o remount,ro /flash")


#
# Copy RC keytable file to rc_keymaps directory
#
def copykeymapfile():
	srcfile = "/storage/.kodi/addons/script.service.argonforty-device/resources/data/argon40.toml"
	dstfile = "/storage/.config/rc_keymaps/argon40.toml"
	if os.path.isfile(dstfile) == True:
		tmpdsthash = getFileHash(dstfile)
		tmpsrchash = getFileHash(srcfile)
		if tmpdsthash == tmpsrchash:
			return()
	try:
		copyfile(srcfile, dstfile)
	except:
		return()
	
#
# Copy RC maps conf file to directory .config
#
def copyrcmapsfile():
	srcfile = "/storage/.kodi/addons/script.service.argonforty-device/resources/data/rc_maps.cfg"
	dstfile = "/storage/.config/rc_maps.cfg"
	if os.path.isfile(dstfile) == True:
		tmpdsthash = getFileHash(dstfile)
		tmpsrchash = getFileHash(srcfile)
		if tmpdsthash == tmpsrchash:
			return()
	try:
		copyfile(srcfile, dstfile)
	except:
		return()

#
# Remove old argon remote LIRC conf file
#
def removelircfile():
	#xbmc.log("copylircfile",level=xbmc.LOGNOTICE)
	dstfile = "/storage/.config/lircd.conf"
	if os.path.isfile(dstfile) == True:
		try:
			os.remove(dstfile)
		except:
			return()

#
# Copy Shutdown script to directory .config
#
def copyshutdownscript():
	#xbmc.log("copyshutdownscript",level=xbmc.LOGNOTICE)
	srcfile = "/storage/.kodi/addons/script.service.argonforty-device/resources/data/shutdown.sh"
	dstfile = "/storage/.config/shutdown.sh"
	if os.path.isfile(dstfile) == True:
		tmpdsthash = getFileHash(dstfile)
		tmpsrchash = getFileHash(srcfile)
		if tmpdsthash == tmpsrchash:
			return()
	try:
		copyfile(srcfile, dstfile)
	except:
		return()

#
# Check file hash
#
def getFileHash(fname):
	try:
		fp = open(fname,"rb")
		content = fp.read()
		fp.close()
		return zlib.crc32(content)
	except:
		return 0

#	
# Cleanup
#

def cleanup():
	# Turn off Fan
	argonregister_setfanspeed(bus, 0)

	# GPIO
	# GPIO.cleanup()
	# gpiozero automatically restores the pin settings at the end of the script
	tmp_lgpio_work_dir.cleanup()

if bus == None:
	checksetup()
else:
	# Respect user-specific remote control settings
	lockfile = "/storage/.config/argon40_rc.lock"
	if os.path.exists(lockfile) == False:
		copykeymapfile()
		copyrcmapsfile()
		removelircfile()
	copyshutdownscript()
