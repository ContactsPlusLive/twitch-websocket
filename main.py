# This file is ugly and needs to be split and organized ;w;

import asyncio
from contextlib import asynccontextmanager
import json
import os
import logging
import sys
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from twitchAPI.twitch import Twitch
from twitchAPI.oauth import  UserAuthenticator
from twitchAPI.type import (
    AuthScope,
    AuthType,
    TwitchAPIException,
)
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.object.eventsub import ChannelPointsCustomRewardRedemptionAddEvent
from twitchAPI.helper import first

load_dotenv()

APP_HOST = os.getenv("APP_HOST", "localhost")
APP_PORT = os.getenv("APP_PORT")
APP_SCHEME = os.getenv("APP_SCHEME", "http")
EVENTSUB_USERNAME = os.getenv("EVENTSUB_USERNAME")
EVENTSUB_URL = os.getenv("EVENTSUB_URL")
EVENTSUB_PORT = int(os.getenv("EVENTSUB_PORT", "8081"))
TWITCH_APP_ID = os.getenv("TWITCH_APP_ID")
TWITCH_APP_SECRET = os.getenv("TWITCH_APP_SECRET")
TARGET_SCOPE = [
    AuthScope.CHANNEL_READ_ADS,
    AuthScope.CHANNEL_READ_GOALS,
    AuthScope.CHANNEL_READ_SUBSCRIPTIONS,
    AuthScope.CHANNEL_READ_POLLS,
    AuthScope.CHANNEL_READ_PREDICTIONS,
    AuthScope.CHANNEL_READ_REDEMPTIONS,
    AuthScope.CHANNEL_MANAGE_REDEMPTIONS,
]

logger = logging.getLogger("uvicorn.error")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await twitch_setup()

    yield

    await close_twitch()

app = FastAPI(lifespan=lifespan)
twitch: Twitch
auth: UserAuthenticator
eventsub: EventSubWebhook

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

def get_url(path: str):
    if APP_PORT:
        return f"{APP_SCHEME}://{APP_HOST}:{APP_PORT}{path}"
    return f"{APP_SCHEME}://{APP_HOST}{path}"


@app.get("/")
async def get_status(request: Request):
    global twitch
    return {
            "success": True,
            "message": "Server is running",
            "twitch": twitch.has_required_auth(AuthType.USER, TARGET_SCOPE),
            "login_url": get_url("/login"),
            "logout_url": get_url("/logout"),
            "eventsub_url": EVENTSUB_URL,
            "eventsub_port": EVENTSUB_PORT,
            "eventsub_username": EVENTSUB_USERNAME
        }
    


@app.get("/login")
async def login(request: Request):
    global twitch, auth
    if twitch.get_user_auth_token() is not None:
        return {
                "success": True,
                "message": "Already authenticated",
                "logout_url": get_url("/logout"),
            }
        
    return {
            "success": True,
            "message": "Please visit the following URL to authenticate",
            "url": auth.return_auth_url(),
        }


@app.get("/login/callback")
async def login_callback(request: Request):
    global token, refresh, eventsub

    state = request.query_params.get("state")
    if state != auth.state:
        return {"success": False, "error": "Invalid state"}

    code = request.query_params.get("code")
    if code is None:
        return {"success": False, "error": "No code provided"}

    try:
        result = await auth.authenticate(user_token=code)
        if result is None:
            return {"success": False, "error": "Failed to authenticate"}

        token, refresh = result

        await twitch.set_user_authentication(token, TARGET_SCOPE, refresh)
    except TwitchAPIException as e:
        logger.error(f"Failed to authenticate: {e}")
        return {"success": False, "error": "Failed to authenticate"}

    with open("user_token.json", "w") as f:
        json.dump({"token": token, "refresh": refresh}, f)

    logger.info("Authenticated with Twitch")
    return {"success": True, "message": "You may now close this window"}


@app.get("/logout")
async def logout(request: Request):
    global twitch
    await twitch.close()
    with open("user_token.json", "w") as f:
        json.dump({"token": "", "refresh": ""}, f)

    await twitch_setup(app)  # type: ignore

    logger.info("Logged out")
    return dict(success=True, message="Logged out")


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await manager.connect(ws)
    while True:
        data = await ws.receive_text()
        await ws.send_text(f"Message text was: {data}")


async def close_twitch():
    global twitch, eventsub
    await twitch.close()


async def on_redeem(data: ChannelPointsCustomRewardRedemptionAddEvent):
    logger.debug(
        f"Redeemed {data.event.reward.title} by {data.event.user_name} with message {data.event.user_input}"
    )
    await manager.broadcast(f"Redeemed {data.event.reward.title} by {data.event.user_name} with message {data.event.user_input}")


async def refresh_callback(token, refresh):
    logger.info("Refreshing token")
    with open("user_token.json", "w") as f:
        json.dump({"token": token, "refresh": refresh}, f)
    return

async def twitch_setup():
    global twitch, auth
    if TWITCH_APP_ID is None or TWITCH_APP_SECRET is None:
        logger.error(
            "Please set TWITCH_APP_ID and TWITCH_APP_SECRET environment variables"
        )
        return

    twitch = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)
    auth = UserAuthenticator(
        twitch, TARGET_SCOPE, url=get_url("/login/callback"), force_verify=True
    )

    twitch.user_auth_refresh_callback = refresh_callback

    try:
        with open("user_token.json", "r") as f:
            data = json.load(f)
            token = data["token"]
            refresh = data["refresh"]
            try:
                await twitch.set_user_authentication(token, TARGET_SCOPE, refresh)
            except TwitchAPIException:
                logger.info("No token found, please authenticate: " + get_url("/login"))
                return
            logger.info("Authenticated with stored token.")
    except FileNotFoundError:
        logger.info("No token found, please authenticate: " + get_url("/login"))
        return

    await eventsub_setup()


async def eventsub_setup():
    global twitch, eventsub
    if EVENTSUB_USERNAME is None or EVENTSUB_URL is None:
        logger.error(
            "Please set EVENTSUB_USERNAME and EVENTSUB_URL environment variables"
        )
        return

    eventsub = EventSubWebhook(
        EVENTSUB_URL, EVENTSUB_PORT, twitch
    )

    user = await first(twitch.get_users(logins=[EVENTSUB_USERNAME]))

    if user is None:
        logger.error(f"User {EVENTSUB_USERNAME} not found")
        return

    await eventsub.unsubscribe_all()

    eventsub.start()

    try:
        await eventsub.listen_channel_points_custom_reward_redemption_add(
            user.id, on_redeem
        )
    except Exception as e:
        logger.error(f"Failed to subscribe to event!")

    logger.info("EventSub setup complete!")

    return
