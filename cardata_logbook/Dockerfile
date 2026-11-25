ARG BUILD_FROM
FROM ${BUILD_FROM}

# DejaVu-fontti PDF-raportteihin (ääkköset ja erikoismerkit osoitteissa)
RUN apk add --no-cache font-dejavu

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
