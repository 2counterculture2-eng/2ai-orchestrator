# IB Gateway service using IBeam
# Provides IBKR Client Portal Web API with automatic TOTP authentication
# Deploy as a separate Railway service
FROM voyz/ibeam:latest

# IBeam listens on port 5000 and proxies to IBKR Client Portal Gateway
EXPOSE 5000

# Required env vars (set in Railway):
# IBEAM_ACCOUNT=takuma2ai9
# IBEAM_PASSWORD=Ibkr2AI2025!
# IBEAM_TOTP_SECRET=5Z2SCOGHM4BU67KZ2BNSJL2ZTIVEMCGE

# Increase TOTP buffer to avoid expiry race conditions
ENV IBEAM_REQUEST_RETRIES=5
ENV IBEAM_PAGE_LOAD_TIMEOUT=60
ENV IBEAM_OAUTH_TIMEOUT=60
