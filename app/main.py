"""Application entrypoint: builds the FastAPI app from the src/ packages.

Run with `uvicorn main:app` from the app/ directory (unchanged).
"""
import logging

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.core.lifespan import lifespan
from src.routes import all_routers

app = FastAPI(title="AI Chat + Prompt Security v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

for _router in all_routers:
    app.include_router(_router)


