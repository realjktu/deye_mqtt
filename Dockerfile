FROM python:3.12-slim

COPY requirements.txt ./
RUN pip3 install -r requirements.txt


COPY *.py ./
