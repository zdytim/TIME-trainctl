# u20 frontend compatibility

The existing JSON API schema is retained. The helper reads `/api/v1/state` and
shows an unknown state when the controller snapshot is unavailable or
unverified. It never grants, caches, or predicts allocation permission. The
backend remains the only bridge to the root-authenticated controller socket.
