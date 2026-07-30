# Built here rather than pulled from a third party: the image is two lines,
# and a proxy every episode's audio passes through is a poor place to inherit
# somebody else's supply chain.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf

# -d keeps it in the foreground so Docker supervises it and its log lands in
# `docker logs` rather than in a file nobody reads.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
