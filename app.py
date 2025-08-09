import os
import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, r2_score

from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# 🔐 Telegram Bot Config
TOKEN = "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo"
WEBHOOK_URL = "https://auto-flow-k6sb.onrender.com"

# 🛠 Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 🌐 Flask App
app = Flask(__name__)
bot = Bot(token=TOKEN)
application = ApplicationBuilder().token(TOKEN).build()

# 📊 Load the data
def load_data(file_path):
    logger.info("Loading data from %s", file_path)
    return pd.read_csv(file_path)

# 🧹 Preprocess the data
def preprocess_data(df):
    logger.info("Preprocessing data")
    df = df.dropna()
    df = pd.get_dummies(df)
    return df

# 🔀 Split the data
def split_data(df, target_column):
    logger.info("Splitting data")
    X = df.drop(target_column, axis=1)
    y = df[target_column]
    return train_test_split(X, y, test_size=0.2, random_state=42)

# 📐 Scale the features
def scale_features(X_train, X_test):
    logger.info("Scaling features")
    scaler = StandardScaler()
    return scaler.fit_transform(X_train), scaler.transform(X_test)

# 🧠 Train the model
def train_model(X_train, y_train):
    logger.info("Training model")
    model = LinearRegression()
    model.fit(X_train, y_train)
    return model

# 📈 Evaluate the model
def evaluate_model(model, X_test, y_test):
    logger.info("Evaluating model")
    y_pred = model.predict(X_test)
    mse = mean_squared_error(y_test, y_pred)
    r2 = r2_score(y_test, y_pred)
    return mse, r2, y_pred

# 📊 Plot results
def plot_results(y_test, y_pred):
    logger.info("Plotting results")
    plt.figure(figsize=(10, 6))
    sns.scatterplot(x=y_test, y=y_pred)
    plt.xlabel('Actual Values')
    plt.ylabel('Predicted Values')
    plt.title('Actual vs Predicted Values')
    plt.savefig("results.png")  # Save instead of show for server compatibility

# 🤖 Telegram message handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Received message: %s", update.message.text)
    await update.message.reply_text("Bot received your message!")

application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 🚪 Webhook route
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    application.update_queue.put(update)
    return "ok"

# 🔗 Set webhook on startup
@app.before_first_request
def set_webhook():
    logger.info("Setting webhook")
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")

# 🚀 Main function
def main():
    file_path = 'data.csv'
    target_column = 'target'

    df = load_data(file_path)
    df = preprocess_data(df)
    X_train, X_test, y_train, y_test = split_data(df, target_column)
    X_train_scaled, X_test_scaled = scale_features(X_train, X_test)
    model = train_model(X_train_scaled, y_train)
