from fastapi import FastAPI
from Routers import RegUser, Auth, GetMe, TenderAnalysis
import os
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch


app = FastAPI()

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-here")

app.include_router(RegUser.router)
app.include_router(Auth.router)
app.include_router(GetMe.router)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="session_id",
    max_age=3600 * 24 * 7,
    same_site="lax",
    https_only=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],   
)


@app.get("/")
def root():
    return {"message": "Hello World"}
