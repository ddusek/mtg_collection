import json
import urllib.parse
from json.decoder import JSONDecodeError
from redis import Redis
from pymongo import MongoClient
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from mtg_collection import constants
from mtg_collection.api import logger
from mtg_collection.database import redis_helper
from mtg_collection.database import mongo_helper
from mtg_collection.database.download import Downloader
from mtg_collection.database.synchronize import Synchronizer
from mtg_collection.database.authentication import Authenticator


# pylint: disable=unused-argument
async def register(request: Request) -> JSONResponse:
    """Register a new user and save him into a session.

    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    params = await request.json()
    auth = Authenticator(MONGO)
    try:
        user_info = auth.register_user(
            params["username"], params["password"], params["email"]
        )
        if user_info["success"]:
            response = JSONResponse({"success": True})
            response.set_cookie("user_token", str(user_info["token"]))
            response.set_cookie("user_id", str(user_info["id"]))
            response.set_cookie("username", str(user_info["username"]))
            return response
        return JSONResponse(
            {
                "success": False,
                "username": user_info["username"],
                "userID": user_info["id"],
            }
        )
    except ValueError as err:
        logger.exception(err)


async def login(request: Request) -> JSONResponse:
    """Login user and save him into a session.

    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    try:
        params = await request.json()
        auth = Authenticator(MONGO)
        try:
            user_info = auth.login_user(params["login"], params["password"])
            if user_info["success"]:
                response = JSONResponse(user_info)
                response.set_cookie("user_token", str(user_info["token"]))
                response.set_cookie("user_id", str(user_info["id"]))
                response.set_cookie("username", str(user_info["username"]))
                return response
            return JSONResponse(
                {
                    "success": False,
                    "username": user_info["username"],
                    "userID": user_info["id"],
                }
            )
        except ValueError as err:
            logger.exception(err)
    except JSONDecodeError as err:
        logger.exception(err)


async def logout(request: Request) -> JSONResponse:
    """Logout user by removing his user cookies and his login token from database.

    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    token = request.cookies.get("user_token")
    user_id = request.cookies.get("user_id")
    if not token or not user_id:
        return JSONResponse({"success": False, "message": "no user is logged in"})

    auth = Authenticator(MONGO)
    logout_info = auth.logout_user(token, user_id)
    if logout_info["success"]:
        response = JSONResponse(logout_info)
        response.delete_cookie("user_token")
        response.delete_cookie("user_id")
        response.delete_cookie("username")
        return response

    return JSONResponse(response)


async def suggest(request: Request) -> JSONResponse:
    """Return auto suggested cards.

    :param text: Text which cards need to contain.
    :type text: str
    :return: List of cards.
    :rtype: JSONResponse
    """
    text = request.path_params["text"]
    try:
        data = redis_helper.get_suggestions(REDIS, text, 20)
        result = redis_helper.format_cards(data)
        return JSONResponse(result)
    except (ConnectionError, TimeoutError) as err:
        logger.exception(err)
    except IndexError as err:
        logger.exception(err)
    except TypeError as err:
        logger.exception(err)


async def editions(request: Request) -> JSONResponse:
    """Return all editions.

    :return: List of all editions.
    :rtype: JSONResponse
    """
    try:
        data = redis_helper.get_all_editions(REDIS)
        data_decoded = [byte.decode("utf-8").removeprefix("edition:") for byte in data]
        result = redis_helper.format_dropdown(data_decoded)
        if result is None:
            logger.warning("'/editions' returned 0 values")
        return JSONResponse(result)
    except (ConnectionError, TimeoutError) as err:
        logger.exception("cannot connect to Redis. %s", err)


async def collections(request: Request) -> JSONResponse:
    """Return all collections.

    :return: List of collections.
    :rtype: JSONResponse
    """
    username = request.cookies.get('username')
    try:
        data = redis_helper.get_all_collections(REDIS, username)
        data_decoded = [byte.decode("utf-8") for byte in data]
        result = redis_helper.format_set_dropdown(data_decoded)
        return JSONResponse(result)
    except (ConnectionError, TimeoutError) as err:
        logger.exception("cannot connect to Redis. %s", err)


async def collection(request: Request) -> JSONResponse:
    """Return all cards from collection by its name.

    :param name: Collection key in Redis.
    :type name: str
    :return: List of card objects.
    :rtype: JSONResponse
    """
    username = request.cookies.get('username')
    collection = request.path_params["name"]
    key = f"{username}:{collection}"
    try:
        data = redis_helper.get_collection(REDIS, key)
        data_decoded = [json.loads(byte.decode("utf-8")) for byte in data]

        result = []
        # Add index, so there is a better value to set as key in Vue loops.
        for i, item in enumerate(data_decoded):
            item["id"] = i
            result.append(item)
        return JSONResponse(result)
    except (ConnectionError, TimeoutError) as err:
        logger.exception("cannot connect to Redis. %s", err)


async def add_card(request: Request) -> JSONResponse:
    """Add card to collection.

    :param collection: Collection key in Redis, where card should be saved.
    :type collection: str
    :param card: Card part of key in Redis.
    :type card: str
    :param edition: Edition part of card key in Redis.
    :type edition: str
    :param units: Number of units to save.
    :type units: int
    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    collection = request.path_params["collection"]
    card = request.path_params["card"]
    edition = request.path_params["edition"]
    units = request.path_params["units"]
    username = request.cookies.get('username')
    if not username:
        err = 'username not saved in cookie'
        logger.exception(err)
        raise ValueError(err)
    try:
        result_redis = redis_helper.add_card_to_redis(
            REDIS, username, collection, card, edition, units
        )
        result_mongo = mongo_helper.add_card_to_mongo(
            MONGO, username, collection, card, edition, units
        )
        result = result_redis['success'] and result_mongo['success']
        return JSONResponse({'success': result})
    except ValueError as err:
        logger.exception(err)
    except (ConnectionError, TimeoutError) as err:
        logger.exception("cannot connect to Redis. %s", err)


async def remove_card(request: Request) -> JSONResponse:
    """Remove card from collection.

    param collection: Collection key in Redis, from where to remove card.
    :type collection: str
    :param card: Card part of key in Redis.
    :type card: str
    :param edition: Edition part of card key in Redis.
    :type edition: str
    :param units: Number of units to remove.
    :type units: int
    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    # collection = request.path_params['collection']
    # card = request.path_params['card']
    # edition = request.path_params['edition']
    # units = request.path_params['units']
    return


async def add_collection(request: Request) -> JSONResponse:
    """Add new collection.

    :param collection: Collection key to add to Redis.
    :type collection: str
    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    collection = request.path_params["collection"]
    username = request.cookies.get("username")
    if not username:
        err = 'username not saved in cookies'
        logger.exception(err)
        raise ValueError(err)
    try:
        result_redis = redis_helper.add_collection_to_redis(REDIS, collection, username)
        result_mongo = mongo_helper.add_collection_to_mongo(MONGO, collection, username)
        result = result_redis["success"] and result_mongo["success"]
        return JSONResponse({"success": result, "message": result_mongo["message"]})
    except (ConnectionError, TimeoutError) as err:
        logger.exception("cannot connect to Redis. %s", err)


async def download_scryfall_cards(request: Request) -> JSONResponse:
    """Download cards bulk data from Scryfall.

    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    result = Downloader().download_scryfall_cards()
    return JSONResponse({"success": result})


async def synchronize_scryfall_cards(request: Request) -> JSONResponse:
    """Synchronize cards from Scryfall to redis.

    :return: {"success": bool}.
    :rtype: JSONResponse
    """
    result = Synchronizer(REDIS).synchronize_database()
    return JSONResponse({"success": result})


# Create middlewares.
middlewares = [
    Middleware(
        CORSMiddleware,
        allow_origins=[
            "https://0.0.0.0:8080",
            "https://localhost:8080",
            "https://127.0.0.1:8080",
        ],
        allow_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        allow_methods=["GET", "POST", "OPTIONS"],
    ),
    Middleware(
        SessionMiddleware,
        secret_key="megasecret",
        same_site="none",
        max_age=365 * 24 * 60 * 60,  # 1 year
        https_only=True,
    ),
]

# Create routes.
routes = [
    Mount(
        "/api",
        routes=[
            Route("/register", register, methods=["POST"]),
            Route("/login", login, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/suggest/{text:str}", suggest),
            Route("/editions", editions),
            Route("/collections", collections),
            Route("/collection/{name:str}", collection),
            Route(
                "/add/{collection:str}/{card:str}/{edition:str}/{units:int}",
                add_card,
                methods=["POST"],
            ),
            Route(
                "/remove/{collection:str}/{card:str}/{edition:str}/{units:int}",
                remove_card,
            ),
            Route("/add/{collection}", add_collection, methods=["POST"]),
            Route("/download/scryfall/cards", download_scryfall_cards),
            Route("/synchronize/scryfall/cards", synchronize_scryfall_cards),
        ],
    )
]

# Start api.
app = Starlette(debug=True, middleware=middlewares, routes=routes)

# Connect to Redis.
REDIS = Redis(
    host=constants.REDIS_HOSTNAME, port=constants.REDIS_PORT, db=constants.REDIS_MAIN_DB
)
# Connect to MongoDB.
MONGO = MongoClient(
    "mongodb://%s:%s@%s"
    % (
        urllib.parse.quote_plus(constants.MONGO_USERNAME),
        urllib.parse.quote_plus(constants.MONGO_PASSWORD),
        constants.MONGO_HOSTNAME,
    ),
    serverSelectionTimeoutMS=3000,
)["mtg-collection"]
