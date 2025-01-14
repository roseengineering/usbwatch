
import socket
import select
import traceback
import argparse
import sys
import os
import fcntl
import glob
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from ctypes import (
    c_uint8, c_uint16, c_uint32, POINTER,
    sizeof, addressof, LittleEndianStructure)

import usb.core
from serial.tools import list_ports

# control transfer timeout
USB_TIMEOUT =  5000

# usb classes codes
USB_CLASS_HUB =          0x09

# hub descriptor types
USB_DT_HUB =             0x29
USB_DT_SUPERSPEED_HUB =  0x2a

# hub port status
USB_PORT_STAT_CONNECTION =  0x0001
USB_PORT_STAT_ENABLE =      0x0002
USB_PORT_STAT_SUSPEND =     0x0004
USB_PORT_STAT_OVERCURRENT = 0x0008
USB_PORT_STAT_RESET =       0x0010
USB_PORT_STAT_POWER =       0x0100
USB_PORT_STAT_LOW_SPEED =   0x0200
USB_PORT_STAT_HIGH_SPEED =  0x0400
USB_PORT_STAT_TEST =        0x0800
USB_PORT_STAT_INDICATOR =   0x1000
USB_PORT_STAT_POWER_SS =    0x0200  # USB 3.0

# hub features
USB_PORT_FEAT_ENABLE =   1   
USB_PORT_FEAT_RESET =    4   
USB_PORT_FEAT_POWER =    8   

# usb ioctl
USB_DIR_OUT =            0x00  # to device
USB_DIR_IN =             0x80  # to host
USB_TYPE_CLASS =         0x20
USB_RECIP_DEVICE =       0x00
USB_RECIP_OTHER =        0x03
USBDEVFS_CONTROL =       0x0c0185500
USBDEVFS_RESET =         0x5514

# usb iotcl requests
USB_REQ_GET_DESCRIPTOR = 0x06
USB_REQ_GET_STATUS =     0x00
USB_REQ_SET_FEATURE =    0x03
USB_REQ_CLEAR_FEATURE =  0x01


# helper functions
###########################

def find(data, name, value):
    for d in data:
        if d.get(name) == value:
            return d

def device_serial(dev):
    try:
        return dev.serial_number
    except:
        pass

def device_product(dev):
    try:
        return dev.product
    except ValueError as e:
        pass

def device_manufacturer(dev):
    try:
        return dev.manufacturer
    except ValueError as e:
        pass


# usb ioctl commands
###########################

class usbdevfs_ctrltransfer(LittleEndianStructure):
    _fields_ = [
        ('bRequestType', c_uint8),
        ('bRequest', c_uint8),
        ('wValue', c_uint16),
        ('wIndex', c_uint16),
        ('wLength', c_uint16),
        ("timeout", c_uint32),
        ("data", POINTER(None))
    ]

def usb_filename(dev):
    return f'/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}'

def usb_reset(fd):
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)

def usb_hub_feature(fd, portnum, feature, value):
    ctrl = usbdevfs_ctrltransfer()
    ctrl.bRequestType = USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_OTHER
    ctrl.bRequest = USB_REQ_SET_FEATURE if value else USB_REQ_CLEAR_FEATURE
    ctrl.wValue = feature
    ctrl.wIndex = portnum
    ctrl.wLength = 0
    ctrl.timeout = USB_TIMEOUT
    ctrl.data = None
    fcntl.ioctl(fd, USBDEVFS_CONTROL, ctrl)

def usb_hub_port_status(fd, portnum, usb_level):
    class usb_port_status (LittleEndianStructure):
        _pack_ = 1
        _fields_ = [
            ('wPortStatus', c_uint16),
            ('wPortChange', c_uint16),
        ]
    data = usb_port_status()
    ctrl = usbdevfs_ctrltransfer()
    ctrl.bRequestType = USB_DIR_IN | USB_TYPE_CLASS | USB_RECIP_OTHER
    ctrl.bRequest = USB_REQ_GET_STATUS
    ctrl.wValue = 0
    ctrl.wIndex = portnum
    ctrl.wLength = sizeof(usb_port_status)
    ctrl.timeout = USB_TIMEOUT
    ctrl.data = addressof(data)
    fcntl.ioctl(fd, USBDEVFS_CONTROL, ctrl)
    pstat = usb_port_status.from_buffer_copy(data)
    port_status = pstat.wPortStatus
    res = []
    if usb_level <= 2: 
        if port_status & USB_PORT_STAT_POWER: res.append('P')
    if usb_level == 3:
        if port_status & USB_PORT_STAT_POWER_SS: res.append('P')
    if port_status & USB_PORT_STAT_CONNECTION: res.append('C')
    if port_status & USB_PORT_STAT_ENABLE: res.append('E')
    if port_status & USB_PORT_STAT_RESET: res.append('R')
    if port_status & USB_PORT_STAT_SUSPEND: res.append('S')
    return res

def usb_hub_numports(fd, usb_level):
    class usb_hub_descriptor(LittleEndianStructure):
        _pack_ = 1
        _fields_ = [
            ('bDescLength', c_uint8),
            ('bDescriptorType', c_uint8),
            ('bNbrPorts', c_uint8),            # number of ports
            ('wHubCharacteristics', c_uint16), # power switching, overcurrent
            ('bPwrOn2PwrGood', c_uint8),       # time to power good (2ms)
            ('bHubContrCurrent', c_uint8)      # max current used (mA)
        ]
    data = usb_hub_descriptor()
    desc_type = USB_DT_SUPERSPEED_HUB if usb_level >= 3 else USB_DT_HUB 
    ctrl = usbdevfs_ctrltransfer()
    ctrl.bRequestType = USB_DIR_IN | USB_TYPE_CLASS | USB_RECIP_DEVICE
    ctrl.bRequest = USB_REQ_GET_DESCRIPTOR
    ctrl.wValue = desc_type << 8
    ctrl.wIndex = 0
    ctrl.wLength = sizeof(usb_hub_descriptor)
    ctrl.timeout = USB_TIMEOUT
    ctrl.data = addressof(data)
    try:
        fcntl.ioctl(fd, USBDEVFS_CONTROL, ctrl)
        hub_descriptor = usb_hub_descriptor.from_buffer_copy(data)
        return hub_descriptor.bNbrPorts
    except Exception:
        pass


# list usb ports
###########################

def parse_location(location):
    location = location.strip().split(':')[0]
    bus, _, port_numbers = location.partition('-')
    bus = int(bus)
    port_numbers = port_numbers.split('.')
    port_numbers = tuple(int(d) for d in port_numbers)
    location = (bus,) + port_numbers
    return location

def update_comports(ports):
    for info in list_ports.comports():
        if info.vid is not None and info.pid is not None:
            location = parse_location(info.location)
            if d := find(ports, 'location', location):
                if d.get('name'):
                    d['name'] = f'{d["name"]} {info.name}' 
                else:
                    d['name'] = info.name

def update_hubs(ports):
    for d in list(ports):
        if d.get('is_hub'):
            usb_level = d['usb_level']
            location = d['location']
            # write access required to perform control transfer
            with open(usb_filename(d['dev']), 'w+') as fd:
                numports = usb_hub_numports(fd, usb_level)
                if numports is None:
                    continue
                d['numports'] = numports
                for portnum in range(1, numports + 1):
                    port_status = usb_hub_port_status(fd, portnum, usb_level)
                    port_location = location + (portnum,)
                    if res := find(ports, 'location', port_location):
                        res['port_status'] = port_status
                    else:
                        ports.append({ 
                            'location': port_location, 
                            'port_status': port_status,
                        })

def list_usbports():
    ports = []
    for dev in usb.core.find(find_all=True):
        usb_level = dev.bcdUSB >> 8
        port_numbers = dev.port_numbers or ()
        location = (dev.bus,) + port_numbers
        manufacturer = device_manufacturer(dev)
        product = device_product(dev)
        if manufacturer:
            manufacturer = manufacturer.strip() 
        if product:
            product = product.strip() 
        d = { 
            'dev': dev,
            'bus': dev.bus,
            'port_number': dev.port_number,
            'address': dev.address,
            'vidpid': (dev.idVendor, dev.idProduct),
            'location': location,
            'usb_level': usb_level,
            'serial_number': device_serial(dev),
            'product': product,
            'manufacturer': manufacturer,
            'is_hub': dev.bDeviceClass == USB_CLASS_HUB,
        }
        ports.append(d)
    update_hubs(ports)
    update_comports(ports)
    return ports

def describe_ports(ports):
    ports.sort(key=lambda d: d['location'])
    data = []
    for d in ports:
        location = d['location']
        is_hub = d.get('is_hub')
        port_status = d.get('port_status')
        name = d.get('name')
        serial_number = d.get('serial_number')
        vidpid = d.get('vidpid')
        manufacturer = d.get('manufacturer')
        product = d.get('product')
        serial_number = d.get('serial_number')
        ###
        if is_hub:
            continue
        address = str(location[0])
        port_numbers = '.'.join(f'{d:02d}' for d in location[1:])
        if port_numbers:
            address = f'{address}-{port_numbers}'
        if not vidpid:
            product = ''
        elif is_hub:
            product = 'Hub'
        elif not product:
            product = '?'
        if serial_number:
            product = f'{product} ({serial_number})'
        if manufacturer:
            product = f'{manufacturer} {product}'
        if name:
            product = f'{name} - {product}'
        vidpid = ':'.join(f'{d:04x}' for d in vidpid) if vidpid else ''
        port_status = '[' + ''.join(port_status or []) + ']'
        line = f'{address:13s} {port_status:5s} {vidpid} {product}'
        data.append(line)
    return data


# INDI server
##################################

class Indiserver:
    BUFFER_SIZE = 4096

    def _buffer_update(self, buf, text):
        d = text.split('\n')
        if not buf:
            buf.append('')
        buf[-1] = buf[-1] + d[0]
        buf.extend(d[1:])           
                
    def _buffer_parse(self, buf):
        n = 0
        for i in range(len(buf)):
            try:
                text = '\n'.join(buf[n:i+1])
                root = ET.fromstring(text)
                if self.verbose:
                    print('------- parse -------')
                    print(text)
                yield root
                n = i + 1
            except ET.ParseError:
                pass
        del buf[:n]

    def _accept_conn(self, conn):
        s = conn.accept()[0]
        self._socklist.append(s)
        self._readbuf[s] = []

    def _close_conn(self, s):
        self._socklist.remove(s)
        del self._readbuf[s]
        s.close()

    def publish(self, root):
        if self._socklist:
            ET.indent(root)
            payload = ET.tostring(root, encoding='latin', xml_declaration=False)
            payload += b'\n'
            if self.verbose:
                print('----- server_publish ------')
                print(payload.decode(), end='')
            for s in self._socklist:
                s.sendall(payload)

    def loop(self, host, port):
        self._readbuf = {}
        self._socklist = []
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            conn.setblocking(0)  # set non-blocking mode
            conn.bind((host, port))
            conn.listen(5)
            while True:
                read_s, write_s, error_s = select.select(self._socklist + [conn], [], self._socklist, 1)
                for s in error_s:
                    self._close_conn(s)
                for s in read_s:
                    if s == conn:
                        self._accept_conn(s)
                    elif chunk := s.recv(self.BUFFER_SIZE):
                        text = chunk.decode('latin')
                        self._buffer_update(self._readbuf[s], text)
                        for root in self._buffer_parse(self._readbuf[s]):
                            self.on_message(root)
                    else:
                        self._close_conn(s)

    def __init__(self, verbose=False):
        hostname = socket.gethostname().split('.')[0]
        self.device = f'USBWATCH_{hostname.upper()}'
        self.name = 'PORT'
        self.state = 'Ok'
        self.group = 'Main Control'
        self.length = None
        self.verbose = verbose
        self.update_values()

    def update_values(self):
        ports = list_usbports()
        arr = describe_ports(ports)
        self.values = [ { 'value': d, 'name': str(i+1) } for i, d in enumerate(arr) ]
 
    def set_property(self):
        attrib = { 'name': self.name, 'state': self.state }
        if self.message:
            attrib['message'] = self.message
        root = ET.Element(f'setTextVector', attrib=attrib, device=self.device)
        for d in self.values:
            attrib = { 'name': d['name'] }
            el = ET.SubElement(root, f'oneText', attrib=attrib)
            el.text = d['value']
        return root

    def define_property(self):
        attrib = { 'perm': 'rw', 'group': self.group, 'name': self.name, 'state': self.state }
        root = ET.Element(f'defTextVector', attrib=attrib, device=self.device)
        for d in self.values:
            attrib = { 'name': d['name'] }
            el = ET.SubElement(root, f'defText', attrib=attrib)
            el.text = str(d['value'])
        return root

    def new_property(self, root):
        try:
            self.state = 'Alert'
            self.message = None
            changes = [ 
                (child.text.strip().lower(), self.values[int(child.attrib['name'])-1]['value'])
                for child in root if child.tag == f'oneText' and child.text and child.text.strip() 
            ]
            if len(changes) == 1:
                command, location = changes[0]
                location = location.split()[0]
                if command == 'reset':
                    soft_reset(location)
                elif command == 'hard':
                    set_feature(location, USB_PORT_FEAT_RESET, 1)
                elif command == 'disable':
                    set_feature(location, USB_PORT_FEAT_ENABLE, 0)
                elif command == 'on':
                    set_feature(location, USB_PORT_FEAT_POWER, 1)
                elif command == 'off':
                    set_feature(location, USB_PORT_FEAT_POWER, 0)
                else:
                    self.message = 'command not recognized'
                    return
            elif changes:
                self.message = 'too many commands, erase those not needed'
                return
            self.state = 'Ok'
            self.update_values()
        except Exception:
            message = traceback.format_exc().strip()
            print(message, file=sys.stderr)
            self.state = 'Alert'
            self.message = message.splitlines()[-1]

    def on_message(self, root):
        device = root.attrib.get('device')
        name = root.attrib.get('name')
        if root.tag == 'getProperties':
            self.publish(self.define_property())
        elif root.tag == 'newTextVector' and device == self.device and name == self.name:
            self.new_property(root)
            self.publish(self.define_property())
            self.publish(self.set_property())


# REST server
################################

class HTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def not_found(self):
        return self.text_response(404, 'Not Found')

    def success(self, text=None):
        text = 'OK' if text is None else text
        return self.text_response(200, text)

    def bad_method(self, text):
        return self.text_response(400, text)

    def text_response(self, code, text):
        text = f'{text}\n' if text else text
        buf = text.encode('latin')
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(buf))
        self.end_headers()
        self.wfile.write(buf)

    def do_GET(self):
        if self.path != '/':
            return self.not_found()
        return self.success(show_ports())

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        buf = self.rfile.read(content_length)
        text = buf.decode('latin')
        result = None
        try:
            if self.path == '/reset':
                soft_reset(text)
            elif self.path == '/hard':
                set_feature(text, USB_PORT_FEAT_RESET, 1)
            elif self.path == '/disable':
                set_feature(text, USB_PORT_FEAT_ENABLE, 0)
            elif self.path == '/on':
                set_feature(text, USB_PORT_FEAT_POWER, 1)
            elif self.path == '/off':
                set_feature(text, USB_PORT_FEAT_POWER, 0)
            elif self.path != '/':
                return self.not_found()
            return self.success(show_ports())
        except Exception:
            message = traceback.format_exc().strip()
            print(message, file=sys.stderr)
            return self.bad_method(message.splitlines()[-1])


################################

def parse_args():
    parser = argparse.ArgumentParser(
        prog=os.path.splitext(os.path.basename(__file__))[0],
        description='Tool to soft reset, hard reset, power on and off, or disable USB ports.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--reset', metavar='LOCATION', help='tell driver to soft reset port')
    parser.add_argument('--hard', metavar='LOCATION', help='tell USB hub to hard reset port')
    parser.add_argument('--disable', metavar='LOCATION', help='tell USB hub to disable port')
    parser.add_argument('--off', metavar='LOCATION', help='tell USB hub to power off port')
    parser.add_argument('--on', metavar='LOCATION', help='tell USB hub to power on port')
    parser.add_argument('-v', '--verbose', action='store_true', help='enable verbose messages')
    group = parser.add_argument_group('server')
    group.add_argument('--rest', action='store_true', help='start REST server')
    group.add_argument('--indi', action='store_true', help='start INDI server')
    group.add_argument('--host', default='0.0.0.0', help='server host')
    group.add_argument('--rest-port', metavar='PORT', type=int, default=80, help='REST server port')
    group.add_argument('--indi-port', metavar='PORT', type=int, default=7624, help='INDI server port')
    return parser.parse_args()

def soft_reset(location):
    ports = list_usbports()
    location = parse_location(location)
    if (d := find(ports, 'location', location)) is None:
        raise ValueError('bad usb port location, port not found')
    if 'dev' not in d:
        raise ValueError('usb device not enumerated or plugged in, use the other commands')
    with open(usb_filename(d['dev']), 'w+') as fd:
        usb_reset(fd)

def set_feature(location, feature, value):
    ports = list_usbports()
    location = parse_location(location)
    if (d := find(ports, 'location', location)) is None:
        raise ValueError('bad usb port location, port not found')
    d = find(ports, 'location', location[:-1])
    if 'dev' not in d:
        raise ValueError('cannot talk to device\'s hub, usb hub never enumerated')
    with open(usb_filename(d['dev']), 'w+') as fd:
        usb_hub_feature(fd, location[-1], feature, value)

def show_ports():
    ports = list_usbports()
    arr = describe_ports(ports)
    text = '\n'.join(arr)
    return text

def command_line(args):
    try:
        if args.reset:
            soft_reset(args.reset)
        elif args.hard:
            set_feature(args.hard, USB_PORT_FEAT_RESET, 1)
        elif args.disable:
            set_feature(args.disable, USB_PORT_FEAT_ENABLE, 0)
        elif args.on:
            set_feature(args.on, USB_PORT_FEAT_POWER, 1)
        elif args.off:
            set_feature(args.off, USB_PORT_FEAT_POWER, 0)
        print(show_ports())
    except Exception:
        message = traceback.format_exc().strip()
        if not args.verbose:
            message = message.splitlines()[-1]
        print(message, file=sys.stderr)


def main():
    args = parse_args()
    if args.rest:
        print(f'starting REST server on {args.host}:{args.rest_port}...', file=sys.stderr)
        with HTTPServer((args.host, args.rest_port), HTTPRequestHandler) as server:
            server.serve_forever()
    elif args.indi:
        print(f'starting INDI server on {args.host}:{args.indi_port}...', file=sys.stderr)
        server = Indiserver(verbose=args.verbose)
        server.loop(args.host, args.indi_port)
    else:
        command_line(args)

if __name__ == '__main__':
    main()


