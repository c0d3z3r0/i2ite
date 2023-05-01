# I2ITE

Tool for talking to ITE ECs via SMBus debug interface (DBGR/SMB).

## Wiring

Check your board´s schematics for SDA/SCL on the battery connector.
D0 of your FTDI interface goes to SCL, while D1+D2 togeter form SDA.

~~~
                                  +---+
+------+                          | 1 |
|   D2 |--+-----------+           | 2 |
|      |  |           |           | 3 |
|   D1 |--+           |           | 4 |
|      |              +-----------| 5 | SDA
|   D0 |--------------------------| 6 | SCL
|      |                          | 7 |
|  GND |--------------------------| 8 | GND
+------+                          | 9 |
                                  +---+
FT2232H/FT4232H/FT232H            Battery connector
                                  (in this example: Clevo L140CU)
~~~

## Dependencies

I2ITE depends on [pyftdi](https://github.com/eblot/pyftdi).
To be able to *really* relax the bus and allow other devices to communicate, you should use this
additional patch: https://github.com/eblot/pyftdi/pull/314/commits/051ccb2d087fe43e92b9d688cf81649276db1d6b

## Example usage

Read some data

~~~
$ ./i2ite.py ftdi://ftdi:4232/2 0xff24
ff24: 07
~~~

Write some data

~~~
$ ./i2ite.py ftdi://ftdi:4232/2 0xffab 0x00
~~~

... or in some python shell like ipython:

~~~
import i2ite
> i = i2ite.I2ITE("ftdi://ftdi:4232/2")
> i.xram_dump(0, 0x10)
      00 01 02 03  04 05 06 07  08 09 0a 0b  0c 0d 0e 0f
      -- -- -- --  -- -- -- --  -- -- -- --  -- -- -- --
0000: 05 b1 11 34  12 b1 00 00  00 00 00 00  00 00 00 00
0010: 00 00 00 00  00 00 00 00  00 00 00 00  00 00 00 00
~~~

## License

Copyright (c) 2021 Michael Niewöhner

This is open source software, licensed under GPLv2. See LICENSE file for details.
