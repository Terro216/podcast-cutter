# Offsite backup image: restic for the encrypted repository, rclone for the
# Yandex Disk backend, on a small Alpine. Mirrors the cinemarr/vaultwarden
# stacks so one operator knows all three — same tools, same pins, same repo
# layout (`yadisk:/backups/<stack>`). Base images are digest-pinned because a
# backup tool silently changing under you is the one dependency you cannot
# afford to float.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic
FROM rclone/rclone:1.74.4@sha256:c61954aaa32328a5486715dd063a81c7879f5195ad3505cd362deddd509dc4a1 AS rclone

FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
RUN apk add --no-cache \
    bash=5.2.37-r0 ca-certificates=20260611-r0 coreutils=9.7-r1 \
    curl=8.14.1-r3 findutils=4.10.0-r0 jq=1.8.1-r0 sqlite=3.49.2-r1 \
    sqlcipher=4.6.1-r0 \
    tar=1.35-r3 util-linux=2.41-r9
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY scripts/backup-container.sh /usr/local/bin/podcast-cutter-backup
COPY scripts/restore-container.sh /usr/local/bin/podcast-cutter-restore
RUN chmod 0755 /usr/local/bin/podcast-cutter-backup \
    /usr/local/bin/podcast-cutter-restore
ENTRYPOINT ["/usr/local/bin/podcast-cutter-backup"]
