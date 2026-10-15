FROM mcr.microsoft.com/playwright/python@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
RUN python -m pip install --no-cache-dir playwright==1.63.0
