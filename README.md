# Twitch⇔Resonite Websocket

A Python script using FastAPI and pyTwitchAPI to achieve an EventSub connection between the Twitch API and Resonite via websocket.

Make sure to set the environment variables as setup in `.env.example`, then run the server just like you would a normal FastAPI instance.

For debugging:
```sh
uvicorn main:app --reload --log-level debug
```