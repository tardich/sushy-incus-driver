FROM quay.io/metal3-io/sushy-tools:latest

# Copy incus driver
COPY sushy-incus-driver /opt/sushy-incus-driver

RUN \
  cd /opt/sushy-incus-driver && \
  python3 -m pip install --upgrade pip wheel build && \
  python3 -m build && \
  python3 -m pip install --no-cache-dir dist/sushy_incus_driver-0.1.4-py3-none-any.whl
  
# Add sitecustomize.py hook so the incus driver is used into account if needed
#COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/

# Entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD []
