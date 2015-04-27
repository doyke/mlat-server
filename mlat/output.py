# -*- mode: python; indent-tabs-mode: nil -*-

import asyncio
import logging
import time
import math
import functools
import socket
import numpy

import mlat.constants
import mlat.geodesy
import mlat.util

# various output methods for multilateration results


def format_time(timestamp):
    return time.strftime("%H:%M:%S", time.gmtime(timestamp)) + ".{0:03.0f}".format(math.modf(timestamp)[0] * 1000)


def format_date(timestamp):
    return time.strftime("%Y/%m/%d", time.gmtime(timestamp))


def csv_quote(s):
    if s is None:
        return ''
    if s.find('\n') == -1 and s.find('"') == -1 and s.find(',') == -1:
        return s
    else:
        return '"' + s.replace('"', '""') + '"'


class LocalCSVWriter(object):
    """Writes multilateration results to a local CSV file"""

    TEMPLATE = '{t:.3f},{address:06X},{callsign},{squawk},{lat:.4f},{lon:.4f},{alt:.0f},{err:.0f},{n},{d},{receivers}\n'
    KTEMPLATE = '{t:.3f},{address:06X},{callsign},{squawk},{lat:.4f},{lon:.4f},{alt:.0f},{err:.0f},{n},{d},{receivers},{klat:.4f},{klon:.4f},{kalt:.0f},{kheading:.0f},{kspeed:.0f},{kvrate:.0f},{kerr:.0f}\n'  # noqa

    def __init__(self, coordinator, filename):
        self.logger = logging.getLogger("csv")
        self.coordinator = coordinator
        self.filename = filename
        self.f = open(filename, 'a')
        self.coordinator.add_output_handler(self.write_result)
        self.coordinator.add_sighup_handler(self.reopen)

    def start(self):
        return mlat.util.completed_future

    def close(self):
        self.coordinator.remove_output_handler(self.write_result)
        self.coordinator.remove_sighup_handler(self.reopen)
        self.f.close()

    def wait_closed(self):
        return mlat.util.completed_future

    def reopen(self):
        try:
            self.f.close()
            self.f = open(self.filename, 'a')
            self.logger.info("Reopened {filename}".format(filename=self.filename))
        except Exception:
            self.logger.exception("Failed to reopen {filename}".format(filename=self.filename))

    def write_result(self, receive_timestamp, address, ecef, ecef_cov, receivers, distinct, kalman_state):
        try:
            lat, lon, alt = mlat.geodesy.ecef2llh(ecef)

            ac = self.coordinator.tracker.aircraft[address]
            callsign = ac.callsign
            squawk = ac.squawk

            if ecef_cov is None:
                err_est = -1
            else:
                var_est = numpy.sum(numpy.diagonal(ecef_cov))
                if var_est >= 0:
                    err_est = math.sqrt(var_est)
                else:
                    err_est = -1

            if kalman_state.valid:
                line = self.KTEMPLATE.format(
                    t=receive_timestamp,
                    address=address,
                    callsign=csv_quote(callsign),
                    squawk=csv_quote(squawk),
                    lat=lat,
                    lon=lon,
                    alt=alt * mlat.constants.MTOF,
                    err=err_est,
                    n=len(receivers),
                    d=distinct,
                    receivers=csv_quote(','.join([receiver.user for receiver in receivers])),
                    klat=kalman_state.position_llh[0],
                    klon=kalman_state.position_llh[1],
                    kalt=kalman_state.position_llh[2] * mlat.constants.MTOF,
                    kheading=kalman_state.heading,
                    kspeed=kalman_state.ground_speed * mlat.constants.MS_TO_KTS,
                    kvrate=kalman_state.vertical_speed * mlat.constants.MS_TO_FPM,
                    kerr=kalman_state.position_error)
            else:
                line = self.TEMPLATE.format(
                    t=receive_timestamp,
                    address=address,
                    callsign=csv_quote(callsign),
                    squawk=csv_quote(squawk),
                    lat=lat,
                    lon=lon,
                    alt=alt * mlat.constants.MTOF,
                    err=err_est,
                    n=len(receivers),
                    d=distinct,
                    receivers=csv_quote(','.join([receiver.user for receiver in receivers])))

            self.f.write(line)

        except Exception:
            self.logger.exception("Failed to write result")
            # swallow the exception so we don't affect our caller


class BasestationClient(object):
    TEMPLATE = 'MSG,{mtype},1,1,{addr:06X},1,{rcv_date},{rcv_time},{now_date},{now_time},{callsign},{altitude},{speed},{heading},{lat},{lon},{vrate},{squawk},{fs},{emerg},{ident},{aog}\n'  # noqa

    def __init__(self, reader, writer, *, coordinator, use_kalman_data, heartbeat_interval=30.0):
        peer = writer.get_extra_info('peername')
        self.host = peer[0]
        self.port = peer[1]
        self.logger = mlat.util.TaggingLogger(logging.getLogger("basestation"),
                                              {'tag': '{host}:{port}'.format(host=self.host,
                                                                             port=self.port)})
        self.reader = reader
        self.writer = writer
        self.coordinator = coordinator
        self.use_kalman_data = use_kalman_data
        self.heartbeat_interval = heartbeat_interval
        self.last_output = time.monotonic()
        self.heartbeat_task = asyncio.async(self.send_heartbeats())
        self.reader_task = asyncio.async(self.read_until_eof())

        self.logger.info("Connection established")
        self.coordinator.add_output_handler(self.write_result)

    def close(self):
        if not self.writer:
            return  # already closed

        self.logger.info("Connection lost")
        self.coordinator.remove_output_handler(self.write_result)
        self.heartbeat_task.cancel()
        self.writer.close()
        self.writer = None

    @asyncio.coroutine
    def wait_closed(self):
        yield from mlat.util.safe_wait([self.heartbeat_task, self.reader_task])

    @asyncio.coroutine
    def read_until_eof(self):
        try:
            while True:
                r = yield from self.reader.read(1024)
                if len(r) == 0:
                    self.logger.info("Client EOF")
                    # EOF
                    self.close()
                    return
        except socket.error:
            self.close()
            return

    @asyncio.coroutine
    def send_heartbeats(self):
        try:
            while True:
                now = time.monotonic()
                delay = self.last_output + self.heartbeat_interval - now
                if delay > 0.1:
                    yield from asyncio.sleep(delay)
                    continue

                self.writer.write(b'\n')
                self.last_output = now

        except socket.error:
            self.close()
            return

    def write_result(self, receive_timestamp, address, ecef, ecef_cov, receivers, distinct, kalman_data):
        try:
            if self.use_kalman_data:
                if not kalman_data.valid:
                    return

                lat, lon, alt = kalman_data.position_llh
                speed = int(round(kalman_data.ground_speed * mlat.constants.MS_TO_KTS))
                heading = int(round(kalman_data.heading))
                vrate = int(round(kalman_data.vertical_speed * mlat.constants.MS_TO_FPM))
            else:
                lat, lon, alt = mlat.geodesy.ecef2llh(ecef)
                speed = ''
                heading = ''
                vrate = ''

            ac = self.coordinator.tracker.aircraft[address]
            callsign = ac.callsign
            squawk = ac.squawk
            altitude = int(round(alt * mlat.constants.MTOF))
            send_timestamp = time.time()

            line = self.TEMPLATE.format(mtype=3,
                                        addr=address,
                                        rcv_date=format_date(receive_timestamp),
                                        rcv_time=format_time(receive_timestamp),
                                        now_date=format_date(send_timestamp),
                                        now_time=format_time(send_timestamp),
                                        callsign=csv_quote(callsign),
                                        squawk=csv_quote(squawk),
                                        lat=round(lat, 4),
                                        lon=round(lon, 4),
                                        altitude=altitude,
                                        speed=speed,
                                        heading=heading,
                                        vrate=vrate,
                                        fs='',
                                        emerg='',
                                        ident='',
                                        aog='')
            self.writer.write(line.encode('ascii'))
            self.last_output = time.monotonic()

        except Exception:
            self.logger.exception("Failed to write result")
            # swallow the exception so we don't affect our caller


def make_basestation_listener(host, port, coordinator, use_kalman_data):
    return mlat.net.MonitoringListener(host, port,
                                       functools.partial(BasestationClient,
                                                         coordinator=coordinator,
                                                         use_kalman_data=use_kalman_data))


def make_basestation_connector(host, port, coordinator, use_kalman_data):
    return mlat.net.MonitoringConnector(host, port, 30.0,
                                        functools.partial(BasestationClient,
                                                          coordinator=coordinator,
                                                          use_kalman_data=use_kalman_data))
