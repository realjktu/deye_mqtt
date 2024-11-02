FROM python:3.12-slim

COPY requirements.txt ./
RUN pip3 install -r requirements.txt

COPY deye_invertor/*.py /deye_invertor/
COPY hikvision_doorbell/*.py /hikvision_doorbell/
COPY tenko_heater/*.py /tenko_heater/
