FROM quay.io/metal3-io/sushy-tools:latest

# Copy incus driver
COPY sushy-incus-driver /opt/sushy-incus-driver
RUN pip install /opt/sushy-incus-driver

# Add sitecustomize.py hook so the incus driver is used into account if needed
COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/

EXPOSE 8000

ENTRYPOINT ["sushy-emulator"]

CMD ["--config", "/etc/sushy/sushy-emulator.conf", "-i", "::", "-p", "8000"]
