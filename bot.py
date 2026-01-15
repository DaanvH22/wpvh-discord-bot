import discord
from discord.ext import commands
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

#command prefix instelling
bot = commands.Bot(command_prefix="!", intents=intents)

#bot token:
TOKEN = os.getenv("DISCORD_TOKEN")

#library voor status van gebruiker:
stand_data = {}

#bot login msg:
@bot.event
async def on_ready():
    print(f"logged in as {bot.user}")

#test command:
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

#command voor staan:
@bot.command()
async def stand(ctx):
    user_id = ctx.author.id    
    if user_id in stand_data:
        if stand_data[user_id]["status"] == "seated":
            timestamp = datetime.now()
            time_dif = (timestamp - stand_data[user_id]["prev_timestamp"]).total_seconds()
            stand_data[user_id]["total_seated"] += time_dif
            stand_data[user_id]["prev_timestamp"] = timestamp
            stand_data[user_id]["status"] = "standing"
            await ctx.send(f"{ctx.author.display_name}, is now standing!")
        else:
            await ctx.send(f"{ctx.author.display_name}, you were already standing!")
    else:
        timestamp = datetime.now()
        stand_data[user_id] = {
            "total_seated": 0,
            "total_standing": 0,
            "prev_timestamp": timestamp,
            "status": "standing"
        }
        await ctx.send(f"{ctx.author.display_name}, is now standing!")

#command voor zitten:
@bot.command()
async def sit(ctx):
    user_id = ctx.author.id    
    if user_id in stand_data:
        if stand_data[user_id]["status"] == "standing":
            timestamp = datetime.now()
            time_dif = (timestamp - stand_data[user_id]["prev_timestamp"]).total_seconds()
            stand_data[user_id]["total_standing"] += time_dif
            stand_data[user_id]["prev_timestamp"] = timestamp
            stand_data[user_id]["status"] = "seated"
            await ctx.send(f"{ctx.author.display_name}, is now sitting!")
        else:
            await ctx.send(f"{ctx.author.display_name}, you were already sitting!")
    else:
        timestamp = datetime.now()
        stand_data[user_id] = {
            "total_seated": 0,
            "total_standing": 0,
            "prev_timestamp": timestamp,
            "status": "seated"
        }
        await ctx.send(f"{ctx.author.display_name}, is now sitting!")

#command voor status-opvraag:
@bot.command()
async def status(ctx):
    user_id = ctx.author.id
    if user_id in stand_data:
        timestamp = datetime.now()
        elapsed_time = (timestamp - stand_data[user_id]["prev_timestamp"]).total_seconds()
        temp_total_standing = stand_data[user_id]["total_standing"]
        temp_total_seated = stand_data[user_id]["total_seated"]
        status = stand_data[user_id]["status"]
        if status == "standing":
            temp_total_standing += elapsed_time
        else:
            temp_total_seated += elapsed_time
        await ctx.send(f"{ctx.author.display_name}, your current status is: **{status}**. You have currently been {status} for {elapsed_time} seconds straight.")
        await ctx.send(f"In total you have spent {temp_total_standing} seconds standing and {temp_total_seated}  seconds seated.")
    else:
        await ctx.send(f"No status found.")

#command voor log out:
@bot.command()
async def end(ctx):
    user_id = ctx.author.id
    if user_id in stand_data:
        if stand_data[user_id]["status"] == "seated":
            timestamp = datetime.now()
            time_dif = (timestamp - stand_data[user_id]["prev_timestamp"]).total_seconds()
            stand_data[user_id]["total_seated"] += time_dif
        elif stand_data[user_id]["status"] == "standing":
            timestamp = datetime.now()
            time_dif = (timestamp - stand_data[user_id]["prev_timestamp"]).total_seconds()
            stand_data[user_id]["total_standing"] += time_dif
        del stand_data[user_id]["prev_timestamp"]
        del stand_data[user_id]["status"]
        await ctx.send(f"{ctx.author.display_name}, you have been signed out")
    else:
        await ctx.send(f"ERROR: user: {ctx.author.display_name} is currently not signed in.")

bot.run(TOKEN)