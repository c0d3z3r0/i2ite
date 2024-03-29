#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

__title__       = "I2ITE"
__description__ = "Tool for talking to ITE ECs via SMBus debug interface (DBGR/SMB)"
__author__      = "Michael Niewöhner"
__email__       = "foss@mniewoehner.de"
__license__     = 'GPL-2.0-or-later'
__copyright__   = 'Copyright (c) 2021 Michael Niewöhner'

import time
from argparse import ArgumentParser
from functools import partial, wraps
from pyftdi.ftdi import Ftdi
from pyftdi.i2c import I2cController
from pyftdi.gpio import GpioAsyncController

# TODO
# - implement eflash reading/writing
# - implement SPI flash reading/writing

'''
ITE SMB/DBGR activation waveform
(not true-to-scale)


                 f = 100kHz
----+            +-------+       +-------+
    |            |       |       |       |
SCL +------------+       +-------+       +--- ... -----------
                 .       .                                    => I2C
                 .       .     f = 200kHz
----+          +---+   +---+   +---+   +---+       +--------- ..
    |          | . |   | . |   |   |   |   |       |
SDA +----------+ . +---+ . +---+   +---+   +- ... -+
           . . . . . . . .
           . . . . . . . .
bytes pos. 1 2 3 4 5 6 7 8
               . .
               . .
               . .
               |-| 1.25 us

               |-----------------------------------|
                             > 11 ms



Bitbang baud rate:    1 / 1.25 us = 800 kHz
Required buffer size: 20 ms / 1.25 us = 2000

Tests showed that the minimum length of the waveform is roughly 11 ms.
To be sure activation works reliably, a length of 20 ms was chosen,
resulting in a 16000 bytes long bitbang pattern.
'''

BITBANG_PATTERN = b'\x00\x00\x02\x03\x01\x01\x03\x02'
BITBANG_LENGTH  = 16000

class ADDR:
    # I2C addresses
    class I2C:
        # unknown 0x09
        CMD                 = 0x5a
        DATA                = 0x35
        BLOCK               = 0x79

    # DBGR space addresses
    class DBGR:
        CHIPIDH             = 0x00
        CHIPIDL             = 0x01
        CHIPVER             = 0x02

        ECINDAR0            = 0x04
        ECINDAR1            = 0x05
        ECINDAR2            = 0x06
        ECINDAR3            = 0x07
        ECINDDR             = 0x08

        BREAK               = 0x27

        PCL                 = 0x2a
        PCM                 = 0x2b
        PCH                 = 0x2c

        XADDRL              = 0x2e
        XADDRH              = 0x2f
        XDATA               = 0x30

        BKP1L               = 0x40
        BKP1M               = 0x41
        BKP1H               = 0x42
        BKP2L               = 0x43
        BKP2M               = 0x44
        BKP2H               = 0x45
        BKP3L               = 0x46
        BKP3M               = 0x47
        BKP3H               = 0x48

    # XRAM addresses
    class XRAM:
        FLHCTRL3R           = 0x1063
        FLHCTRL3R_FFSPITRI  = 1 << 0
        FLHCTRL3R_SIFE      = 1 << 3
        SLVISELR            = 0x1c34
        SLVISELR_OVRSMDBG   = 1 << 4
        ETWCTRL             = 0x1f05
        ETWCTRL_EWDSCEN     = 1 << 5
        ETWCTRL_EWDSCMS     = 1 << 4
        RSTC1               = 0x2007
        RSTC1_RGPIO         = 1 << 1
        SFR                 = 0xbf00
        IRAM                = 0xff00

class BREAK:
    NONE                    = 0 << 0
    ANY                     = 1 << 0
    MOVX                    = 1 << 1
    BRANCH                  = 1 << 2
    BRANCH_NO_DJNZ_CJNE     = 1 << 3
    NOP                     = 1 << 4
    RESET                   = 1 << 7

def hexdump(self, read_func, start, end):
    # round start down to 16 byte boundary
    start -= start % 0x10
    alen = 4 if end <= 0x10000 else 8

    # read 16 byte per line
    for yaddr in range(start, end, 0x10):
        if yaddr & 0xff == 0x00:
            if yaddr & ~0xff:
                print()
            print(" " * (alen + 2), end="")
            print("00 01 02 03  04 05 06 07  08 09 0a 0b  0c 0d 0e 0f")
            print(" " * (alen + 2), end="")
            print("-- -- -- --  -- -- -- --  -- -- -- --  -- -- -- --")

        data = [read_func(addr, relax=False, keep_addrh=True)
                for addr in range(yaddr, yaddr + 0x10)]

        # cut in chunks of 4 byte each
        zip_data = zip(*[iter(data)]*4)
        # format data: ff ff ff ff  ff ff ff ff  ff ff ff ff  ff ff ff ff
        hex_data = '  '.join((map(lambda x: ' '.join(map("{:02x}".format, x)), zip_data)))

        print(f'{yaddr:0{alen}x}: {hex_data}')

    self.relax()

def connected(func, *args, **kwargs):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.connected:
            raise(Exception("Error: not connected"))

        return func(self, *args, **kwargs)

    return wrapper

def limit_addr(_min, _max):
    def dec_limit_addr(func):
        @wraps(func)
        def wrapper(self, addr, *args, **kwargs):
            if not _min <= addr <= _max:
                name = func.__name__.split("_")[0].upper()
                raise(Exception(f"Error: {name} address invalid. Range is {_min} <= addr <= {_max}"))
            return func(self, addr, *args, **kwargs)
        return wrapper
    return dec_limit_addr


class I2ITE:

    def __init__(self, url, frequency=2000000):
        self.url = url
        self.con = None
        self._flash_enabled = False
        self.connected = False
        self.frequency = frequency
        self._xaddrh = -1
        self._breakpoints = [-1, -1, -1]

        self._dumpable = ['dbgr', 'xram', 'sfr', 'iram']
        for d in self._dumpable:
            read_func = getattr(self, f'{d}_read')
            setattr(self, f'{d}_dump', partial(hexdump, self, read_func))

    def close(self):
        if self.connected:
            self.con.close(freeze=True)
            self.connected = False

    def _send_dbgr_waveform(self):
        if self.connected:
            raise(Exception("Error: already connected"))

        wave = BITBANG_PATTERN * int(BITBANG_LENGTH / 8)

        g = GpioAsyncController()
        g.configure(self.url)
        g.ftdi.reset()
        g.ftdi.write_data_set_chunksize(32 * 1024)
        g.set_frequency(800000)
        g.set_direction(0xff, 3)
        g.write(wave)
        # wait a bit so we don't flush too early
        time.sleep(0.005)
        g.ftdi.purge_buffers()
        g.set_direction(0xff, 0)
        g.close()

    def open(self):
        if self.connected:
            self.close()

        self._send_dbgr_waveform()

        self.con = I2cController()
        self.con.configure(self.url, frequency=self.frequency)
        self.con.ftdi.set_latency_timer(1)

        try:
            self.connected = True
            print(f"Connected to {hex(self.chipid)}")

        except:
            self.connected = False
            self.con.close(freeze=True)

            raise(Exception("Error: connection failed"))

    def connect(self):
        self.open()

    def relax(self):
        self.con._do_epilog()

    @property
    @connected
    def chipid(self):
        chipid  = self.dbgr_read(ADDR.DBGR.CHIPIDH) << 8
        chipid |= self.dbgr_read(ADDR.DBGR.CHIPIDL)

        if chipid in (0x0000, 0xffff):
            raise(Exception("Error: Invalid chipid"))

        return chipid

    @property
    @connected
    def chipver(self):
        chipver = self.dbgr_read(ADDR.DBGR.CHIPVER) & 0x0f

        return chipver

    @property
    @connected
    def flash_size(self):
        # Note: decoding is chip-specific
        flash_size = self.dbgr_read(ADDR.DBGR.CHIPVER) & 0xf0

        return flash_size

    @connected
    @limit_addr(0x00, 0xff)
    def dbgr_read(self, addr, relax=True):
        self.con.write(ADDR.I2C.CMD, [addr], relax=False)
        data = self.con.read(ADDR.I2C.DATA, relax=relax)[0]

        return data

    @connected
    @limit_addr(0x00, 0xff)
    def dbgr_write(self, addr, data, relax=True):
        self.con.write(ADDR.I2C.CMD, [addr], relax=False)
        self.con.write(ADDR.I2C.DATA, [data], relax=relax)

    @connected
    @limit_addr(0x0000, 0xffff)
    def xram_read(self, addr, relax=True, keep_addrh=False):
        addrh = addr >> 8 & 0xff
        # save transfers by skipping equal values for addrh
        # to speed up blockwise dumping
        if not keep_addrh or addrh != self._xaddrh:
            self._xaddrh = addrh
            self.dbgr_write(ADDR.DBGR.XADDRH, addrh, relax=False)

        self.dbgr_write(ADDR.DBGR.XADDRL, addr & 0xff, relax=False)
        data = self.dbgr_read(ADDR.DBGR.XDATA, relax=relax)

        return data

    @connected
    @limit_addr(0x0000, 0xffff)
    def xram_write(self, addr, data, relax=True):
        self.dbgr_write(ADDR.DBGR.XADDRH, addr >> 8, relax=False)
        self.dbgr_write(ADDR.DBGR.XADDRL, addr & 0xff, relax=False)
        self.dbgr_write(ADDR.DBGR.XDATA,  data, relax=relax)

    @connected
    @limit_addr(0x80, 0xff)
    def sfr_read(self, addr, relax=True):
        addr += ADDR.XRAM.SFR
        data = self.xram_read(addr, relax=relax)

        return data

    @connected
    @limit_addr(0x80, 0xff)
    def sfr_write(self, addr, data, relax=True):
        addr += ADDR.XRAM.SFR
        self.xram_write(addr, data, relax=relax)

    @connected
    @limit_addr(0x00, 0xff)
    def iram_read(self, addr, relax=True):
        addr += ADDR.XRAM.IRAM
        data = self.xram_read(addr, relax=relax)

        return data

    @connected
    @limit_addr(0x00, 0xff)
    def iram_write(self, addr, data, relax=True):
        addr += ADDR.XRAM.IRAM
        self.xram_write(addr, data, relax=relax)

    @connected
    def disable_watchdog(self):
        reg = ADDR.XRAM.ETWCTRL_EWDSCEN | ADDR.XRAM.ETWCTRL_EWDSCMS
        self.xram_write(ADDR.XRAM.ETWCTRL, reg)

    @connected
    def ecindar_addr(self, addr, relax=True):
        self.dbgr_write(ADDR.DBGR.ECINDAR3, addr >> 24 & 0xff, relax=False)
        self.dbgr_write(ADDR.DBGR.ECINDAR2, addr >> 16 & 0xff, relax=False)
        self.dbgr_write(ADDR.DBGR.ECINDAR1, addr >>  8 & 0xff, relax=False)
        self.dbgr_write(ADDR.DBGR.ECINDAR0, addr       & 0xff, relax=relax)

    @connected
    def ecindar_read(self, addr, relax=True):
        self.ecindar_addr(addr, relax=False)
        return self.dbgr_read(ADDR.DBGR.ECINDDR, relax=relax)

    @connected
    def ecindar_write(self, addr, data, relax=True):
        self.ecindar_addr(addr, relax=False)
        self.dbgr_write(ADDR.DBGR.ECINDDR, data, relax=relax)

    @connected
    def flash_enter_follow_mode(self, ext=False):
        addr = 0x7ffffe00 | (int(ext) << 31)
        self.ecindar_write(addr, 0x00)

    @connected
    def flash_exit_follow_mode(self):
        self.ecindar_addr(0x00000000)

    def _flash_id(self, ext=False):
        if ext:
            reg = self.xram_read(ADDR.XRAM.FLHCTRL3R)
            reg &= ~ADDR.XRAM.FLHCTRL3R_FFSPITRI
            reg |= ADDR.XRAM.FLHCTRL3R_SIFE
            self.xram_write(ADDR.XRAM.FLHCTRL3R, reg)
        self.flash_enter_follow_mode(ext)
        addr = 0x7ffffd00 | (int(ext) << 31)
        self.ecindar_write(addr, 0x9f)
        id = bytes(self.con.read(ADDR.I2C.DATA, 3))
        self.flash_exit_follow_mode()
        return id

    @property
    @connected
    def flash_id(self):
        return self._flash_id(False)

    @property
    @connected
    def flash_id_ext(self):
        return self._flash_id(True)

    @connected
    def ec_stop(self):
        self.flash_enter_follow_mode()
        self.flash_exit_follow_mode()

    @connected
    def ec_gpio_reset(self):
        self.xram_write(ADDR.XRAM.RSTC1, ADDR.XRAM.RSTC1_GPIO)

    @connected
    def ec_reset(self):
        self.dbgr_write(ADDR.DBGR.BREAK, BREAK.RESET)

    @connected
    def ec_break(self, typ=BREAK.ANY):
        self.dbgr_write(ADDR.DBGR.BREAK, typ)

    @connected
    def ec_continue(self):
        self.dbgr_write(ADDR.DBGR.BREAK, BREAK.NONE)

    @connected
    def ec_breakpoint_set(self, id, addr):
        if id not in range(3):
            raise(Exception("Error: invalid breakpoint id"))
        if addr > 0x3ffff:
            raise(Exception("Error: invalid breakpoint address"))

        self.dbgr_write(ADDR.DBGR.BKP1H + (id * 3), addr >> 16 & 0x03, relax=False)
        self.dbgr_write(ADDR.DBGR.BKP1M + (id * 3), addr >>  8 & 0xff, relax=False)
        self.dbgr_write(ADDR.DBGR.BKP1L + (id * 3), addr       & 0xff, relax=True)
        self._breakpoints[id] = addr

    @property
    @connected
    def ec_breakpoints(self):
        return self._breakpoints

    @connected
    def ec_breakpoint_add(self, addr):
        if addr in self.ec_breakpoints:
            return

        try:
            id = self.ec_breakpoints.index(-1)
            self.ec_breakpoint_set(id, addr)

        except ValueError:
            raise(Exception("Error: no unused breakpoint available"))

    @connected
    def ec_breakpoint_remove(self, addr):
        try:
            id = self.ec_breakpoints.index(addr)
            self.dbgr_write(ADDR.DBGR.BKP1H + (id * 3), 0xff, relax=True)
            self._breakpoints[id] = -1

        except ValueError:
            raise(Exception("Error: breakpoint is not set"))

    @connected
    def ec_breakpoints_clear(self):
        for i in range(3):
            self.dbgr_write(ADDR.DBGR.BKP1H + (i * 3), 0xff, relax=False)

        self._breakpoints = [-1, -1, -1]
        self.relax()

    @property
    @connected
    def ec_pc(self):
        pc  = self.dbgr_read(ADDR.DBGR.PCH, relax=False) << 16 & 0x30000
        pc |= self.dbgr_read(ADDR.DBGR.PCM, relax=False) <<  8
        pc |= self.dbgr_read(ADDR.DBGR.PCL, relax=True)

        return pc

    @connected
    def dbgr_disable(self):
        self.xram_write(ADDR.XRAM.SLVISELR, ADDR.XRAM.SLVISELR_OVRSMDBG)


def main():
        argp = ArgumentParser("I2ITE", description=__description__)

        argp.add_argument('device', nargs='?', default='ftdi:///?', help='ftdi url')
        argp.add_argument('--freq', default=2000000, help='I2C frequency')
        argp.add_argument('-d',  action='store_true', help='dump all XDATA')
       #argp.add_argument('-df', action='store_true', help='dump flash')
        rw = argp.add_argument_group()
        rw.add_argument('addr', nargs='?',            help='XDATA address')
        rw.add_argument('data', nargs='?',            help='XDATA data to be written')
        args = argp.parse_args()

        if not (args.d or args.device):
            argp.error('Need either -d or addr')
        if not args.device:
            argp.error('Ftdi device not specified')

        i2ite = I2ITE(args.device)
        i2ite.connect()

        if args.d:
            i2ite.dump(0, 0x10000)

       #elif args.df:
       #    i2ite.dump_flash(0, 128*1024)

        elif args.addr:
            addr = int(args.addr, 0)

            if args.data:
                data = int(args.data, 0)
                i2ite.write(addr, data)

            else:
                data = i2ite.read(addr)
                print(f'{addr:04x}: {data:02x}')

        i2ite.close()


if __name__ == '__main__':
    main()
