FROM quay.io/metal3-io/sushy-tools:latest

# Copy incus driver
COPY sushy-incus-driver /opt/sushy-incus-driver
RUN pip install /opt/sushy-incus-driver

# Add sitecustomize.py hook so the incus driver is used into account if needed
COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/

# Entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD []
