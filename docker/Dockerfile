# Where each conversation's Claude runs when SANDBOX_DOCKER is on (the default).
# Playwright's image brings Node and browsers, so end-to-end tests run in here too.
FROM mcr.microsoft.com/playwright:v1.56.1-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl gh git jq openssh-client python3 python3-pip \
        python3-venv ripgrep unzip zip \
    && rm -rf /var/lib/apt/lists/*

# The host user, under the same uid and home: files keep their owner, and ssh, git and
# Claude find their settings in the mounted home paths.
ARG USER_NAME USER_ID GROUP_ID HOME_DIR
RUN old="$(getent passwd "$USER_ID" | cut -d: -f1)"; \
    if [ -n "$old" ]; then userdel -r "$old" 2>/dev/null || userdel "$old"; fi; \
    getent group "$GROUP_ID" >/dev/null || groupadd -g "$GROUP_ID" "$USER_NAME"; \
    useradd -u "$USER_ID" -g "$GROUP_ID" -d "$HOME_DIR" -m -s /bin/bash "$USER_NAME"
