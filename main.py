import random
import logging
import subprocess
import sys
import os
import re
import time
import concurrent.futures
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
from discord.ui import View, Button, Select
import psutil
import datetime
import json
from typing import Dict, List, Optional

# Configuration
TOKEN = 'your discord bot token'
SERVER_LIMIT = 1
DATABASE_FILE = 'database.json'
LOG_FILE = 'bot.log'
ADMIN_IDS = [your_dc_id]

# Available Docker images with metadata
DOCKER_IMAGES = {
    "nxh-i7": {
        "name": "nxh-i7-docker-image",
        "display_name": "NXH-i7 Premium",
        "description": "High-performance Ubuntu 22.04 with advanced tools",
        "ram": "12GB",
        "cpu": "4 cores"
    },
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

class ImageSelectView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.selected_image = None
        
        # Create a dropdown for image selection
        select = Select(
            placeholder="Choose your premium OS...",
            options=[
                discord.SelectOption(
                    label=img["display_name"],
                    description=img["description"],
                    value=img_name
                ) for img_name, img in DOCKER_IMAGES.items()
            ]
        )
        select.callback = self.select_callback
        self.add_item(select)
        
        # Add deploy button
        deploy_button = Button(label="Deploy Now", style=discord.ButtonStyle.green, emoji="✨")
        deploy_button.callback = self.deploy_callback
        self.add_item(deploy_button)
    
    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your deployment! 😊", ephemeral=True)
            return
            
        self.selected_image = interaction.data['values'][0]
        img_data = DOCKER_IMAGES[self.selected_image]
        
        embed = discord.Embed(
            title="🌟 Image Selected!",
            description=f"**{img_data['display_name']}** is ready for you!",
            color=0xff69b4
        )
        embed.add_field(name="Description", value=img_data["description"], inline=False)
        embed.add_field(name="Resources", value=f"💎 {img_data['ram']} RAM | ⚡ {img_data['cpu']} CPU", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def deploy_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your deployment! 😊", ephemeral=True)
            return
            
        if not self.selected_image:
            await interaction.response.send_message("Please select an image first! 😊", ephemeral=True)
            return
            
        await interaction.response.defer()
        await create_server_task(interaction, self.selected_image)
        self.stop()

# Database functions
def load_database() -> Dict:
    if not os.path.exists(DATABASE_FILE):
        return {}
    
    with open(DATABASE_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_database(data: Dict):
    with open(DATABASE_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def add_to_database(user_id: str, container_id: str, ssh_command: str, image_name: str):
    data = load_database()
    
    if user_id not in data:
        data[user_id] = []
    
    data[user_id].append({
        "container_id": container_id,
        "ssh_command": ssh_command,
        "image": image_name,
        "created_at": datetime.datetime.now().isoformat(),
        "status": "running"
    })
    
    save_database(data)

def remove_from_database(container_id: str):
    data = load_database()
    
    for user_id, containers in data.items():
        data[user_id] = [c for c in containers if c["container_id"] != container_id]
    
    save_database(data)

def update_container_status(container_id: str, status: str):
    data = load_database()
    
    for user_id, containers in data.items():
        for container in containers:
            if container["container_id"] == container_id:
                container["status"] = status
    
    save_database(data)

def get_user_containers(user_id: str) -> List[Dict]:
    data = load_database()
    return data.get(str(user_id), [])

def count_user_containers(user_id: str) -> int:
    return len(get_user_containers(user_id))

def get_container_info(container_id: str) -> Optional[Dict]:
    data = load_database()
    
    for user_id, containers in data.items():
        for container in containers:
            if container["container_id"] == container_id:
                container["user_id"] = user_id
                return container
    return None

# Docker helper functions
async def get_container_stats(container_id: str) -> Dict:
    try:
        container = client.containers.get(container_id)
        stats = container.stats(stream=False)
        
        cpu_percent = 0.0
        memory_usage = 0
        memory_limit = 0
        
        if 'cpu_stats' in stats and 'precpu_stats' in stats:
            cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
            system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
            
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * len(stats['cpu_stats']['cpu_usage']['percpu_usage']) * 100
        
        if 'memory_stats' in stats:
            memory_usage = stats['memory_stats'].get('usage', 0)
            memory_limit = stats['memory_stats'].get('limit', 1)
        
        return {
            'cpu_percent': round(cpu_percent, 2),
            'memory_usage': memory_usage,
            'memory_limit': memory_limit,
            'memory_percent': round((memory_usage / memory_limit) * 100, 2) if memory_limit else 0,
            'online': container.status == 'running'
        }
    except Exception as e:
        logger.error(f"Error getting stats for container {container_id}: {e}")
        return None

async def capture_ssh_session_line(process) -> Optional[str]:
    try:
        while True:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output:
                return output.split("ssh session:")[1].strip()
    except asyncio.TimeoutError:
        logger.warning("Timeout while waiting for SSH session")
    except Exception as e:
        logger.error(f"Error capturing SSH session: {e}")
    return None

async def execute_command(command: str) -> tuple:
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return stdout.decode(), stderr.decode()
    except Exception as e:
        logger.error(f"Error executing command '{command}': {e}")
        return "", str(e)

# Bot events
@bot.event
async def on_ready():
    change_status.start()
    logger.info(f'✨ Bot is ready! Logged in as {bot.user}')
    try:
        await bot.tree.sync()
        logger.info("✨ Commands synced successfully!")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@tasks.loop(seconds=20)
async def change_status():
    try:
        data = load_database()
        total_instances = sum(len(containers) for containers in data.values())
        
        statuses = [
            f"💎 {total_instances} premium servers",
            f"✨ with {len(DOCKER_IMAGES)} awesome options",
            "😊 Type /help for magic commands"
        ]
        
        current_status = statuses[int(time.time()) % len(statuses)]
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=current_status))
    except Exception as e:
        logger.error(f"Failed to update status: {e}")

# Command functions
async def create_server_task(interaction: discord.Interaction, image_name: str):
    user = str(interaction.user.id)
    
    if count_user_containers(user) >= SERVER_LIMIT:
        embed = discord.Embed(
            title="💖 Limit Reached!",
            description=f"You can only have {SERVER_LIMIT} premium instances at a time.",
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)
        return
    
    image_data = DOCKER_IMAGES.get(image_name)
    if not image_data:
        embed = discord.Embed(
            title="🥺 Invalid Image",
            description="The selected premium image is not available.",
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)
        return
    
    # Send initial embed with loading animation
    embed = discord.Embed(
        title=f"🚀 Deploying {image_data['display_name']} Server",
        description="✨ Creating your premium server... This may take a moment.",
        color=0x9370db
    )
    embed.add_field(name="Status", value="🔄 Initializing...", inline=False)
    embed.set_footer(text="✨ Made with love for you!")
    message = await interaction.followup.send(embed=embed)
    
    container_id = None
    try:
        # Step 1: Pull the image if not exists
        embed.set_field_at(0, name="Status", value="🔍 Checking premium image...", inline=False)
        await message.edit(embed=embed)
        
        try:
            client.images.get(image_data['name'])
        except docker.errors.ImageNotFound:
            embed.set_field_at(0, name="Status", value="⬇️ Downloading premium image...", inline=False)
            await message.edit(embed=embed)
            
            try:
                client.images.pull(image_data['name'])
            except docker.errors.DockerException as e:
                logger.error(f"Error pulling image {image_data['name']}: {e}")
                raise Exception(f"Failed to download premium image: {e}")
        
        # Step 2: Create container
        embed.set_field_at(0, name="Status", value="🛠️ Creating your premium server...", inline=False)
        await message.edit(embed=embed)
        
        try:
            container = client.containers.run(
                image_data['name'],
                detach=True,
                tty=True,
                mem_limit='8g',
                cpu_quota=400000,
                cpu_shares=1024,
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
            )
            container_id = container.id
        except docker.errors.DockerException as e:
            logger.error(f"Error creating container: {e}")
            raise Exception(f"Failed to create premium server: {e}")
        
        # Step 3: Start SSH session
        embed.set_field_at(0, name="Status", value="🔑 Generating secure SSH access...", inline=False)
        await message.edit(embed=embed)
        
        try:
            exec_cmd = await asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "tmate", "-F",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            ssh_session_line = await capture_ssh_session_line(exec_cmd)
            
            if not ssh_session_line:
                raise Exception("Failed to generate SSH session")
        except Exception as e:
            logger.error(f"Error generating SSH session: {e}")
            try:
                container = client.containers.get(container_id)
                container.stop()
                container.remove()
            except:
                pass
            raise Exception(f"Failed to generate SSH session: {e}")
        
        # Step 4: Finalize
        add_to_database(user, container_id, ssh_session_line, image_name)
        
        # Create success embed
        success_embed = discord.Embed(
            title=f"🎉 {image_data['display_name']} Server Ready!",
            description="✨ Your premium server is live and ready!",
            color=0x00ff7f
        )
        success_embed.add_field(
            name="🔐 Direct SSH Access",
            value=f"```{ssh_session_line}```",
            inline=False
        )
        success_embed.add_field(
            name="💎 Resources",
            value=f"{image_data['ram']} RAM | {image_data['cpu']} CPU",
            inline=True
        )
        success_embed.add_field(
            name="⚙️ Management",
            value=f"Use `/stop {container_id[:12]}` to pause this server",
            inline=True
        )
        success_embed.set_footer(text=f"✨ Server ID: {container_id[:12]} | Made with 💖")
        
        # Send to user's DMs
        try:
            await interaction.user.send(embed=success_embed)
        except discord.Forbidden:
            logger.warning(f"Could not send DM to user {interaction.user.id}")
        
        # Update original message
        embed.title = f"✅ Deployment Complete!"
        embed.description = f"✨ {image_data['display_name']} server is ready!"
        embed.set_field_at(0, name="Status", value="✔️ Completed Successfully!", inline=False)
        embed.color = 0x00ff7f
        embed.add_field(
            name="💌 Next Steps",
            value="Check your DMs for SSH access details!",
            inline=False
        )
        await message.edit(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in deployment: {e}")
        
        # Cleanup if container was created
        if container_id:
            try:
                container = client.containers.get(container_id)
                container.stop()
                container.remove()
                remove_from_database(container_id)
            except:
                pass
        
        error_embed = discord.Embed(
            title="🥺 Deployment Failed",
            description=str(e),
            color=0xff69b4
        )
        error_embed.add_field(
            name="Status",
            value="Failed - Please try again later 😊",
            inline=False
        )
        
        await message.edit(embed=error_embed)

async def manage_server(interaction: discord.Interaction, action: str, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🥺 Server Not Found",
            description="No premium server found with that ID.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Check permissions
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🥺 Permission Denied",
            description="You don't have permission to manage this premium server.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        
        if action == "start":
            container.start()
            status = "started"
            update_container_status(container_id, "running")
        elif action == "stop":
            container.stop()
            status = "stopped"
            update_container_status(container_id, "stopped")
        elif action == "restart":
            container.restart()
            status = "restarted"
            update_container_status(container_id, "running")
        elif action == "remove":
            container.stop()
            container.remove()
            remove_from_database(container_id)
            status = "removed"
        else:
            raise ValueError("Invalid action")
        
        embed = discord.Embed(
            title=f"✨ Server {status.capitalize()}!",
            description=f"Premium server `{container_id[:12]}` has been {status}.",
            color=0x00ff7f
        )
        
        if action != "remove":
            stats = await get_container_stats(container_id)
            if stats:
                embed.add_field(
                    name="📊 Resources",
                    value=f"⚡ CPU: {stats['cpu_percent']}% | 💾 Memory: {stats['memory_percent']}%",
                    inline=False
                )
        
        await interaction.response.send_message(embed=embed)
        
        if action in ["start", "restart"]:
            # Regenerate SSH session after restart
            try:
                exec_cmd = await asyncio.create_subprocess_exec(
                    "docker", "exec", container_id, "tmate", "-F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                ssh_session_line = await capture_ssh_session_line(exec_cmd)
                
                if ssh_session_line:
                    dm_embed = discord.Embed(
                        title=f"🔑 New SSH Session for {image_data.get('display_name', 'Server')}",
                        description=f"```{ssh_session_line}```",
                        color=0x00ff7f
                    )
                    dm_embed.add_field(
                        name="Server ID",
                        value=container_id[:12],
                        inline=False
                    )
                    dm_embed.set_footer(text="✨ Made with 💖")
                    await interaction.user.send(embed=dm_embed)
            except Exception as e:
                logger.error(f"Error regenerating SSH session: {e}")
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="🥺 Server Not Found",
            description="The premium server no longer exists.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed)
        remove_from_database(container_id)
    except docker.errors.DockerException as e:
        embed = discord.Embed(
            title="🥺 Error Managing Server",
            description=str(e),
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed)

async def regen_ssh_command(interaction: discord.Interaction, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🥺 Server Not Found",
            description="No premium server found with that ID.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🥺 Permission Denied",
            description="You don't have permission to manage this premium server.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        if container.status != 'running':
            raise Exception("Premium server is not running")
        
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        
        if not ssh_session_line:
            raise Exception("Failed to generate SSH session")
        
        # Update the database with new SSH command
        data = load_database()
        for user_id, containers in data.items():
            for container in containers:
                if container["container_id"] == container_id:
                    container["ssh_command"] = ssh_session_line
        save_database(data)
        
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        
        embed = discord.Embed(
            title=f"🔑 New SSH Session for {image_data.get('display_name', 'Server')}",
            description=f"```{ssh_session_line}```",
            color=0x00ff7f
        )
        embed.add_field(
            name="Server ID",
            value=container_id[:12],
            inline=False
        )
        embed.set_footer(text="✨ Made with 💖")
        
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            embed=discord.Embed(
                description="✨ New SSH session generated! Check your DMs! 😊",
                color=0x00ff7f
            )
        )
    
    except Exception as e:
        embed = discord.Embed(
            title="🥺 Error Generating SSH Session",
            description=str(e),
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)

async def show_instance_info(interaction: discord.Interaction, container_id: str):
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🥺 Server Not Found",
            description="No premium server found with that ID.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    user = str(interaction.user.id)
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🥺 Permission Denied",
            description="You don't have permission to view this premium server.",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        stats = await get_container_stats(container_id)
        
        embed = discord.Embed(
            title=f"✨ {image_data.get('display_name', 'Server')} Details",
            color=0x9370db
        )
        embed.add_field(
            name="🆔 Server ID",
            value=container_id[:12],
            inline=True
        )
        embed.add_field(
            name="📊 Status",
            value=container.status.capitalize(),
            inline=True
        )
        embed.add_field(
            name="📅 Created",
            value=datetime.datetime.fromisoformat(container_info['created_at']).strftime('%Y-%m-%d %H:%M'),
            inline=True
        )
        
        if stats:
            embed.add_field(
                name="⚡ CPU Usage",
                value=f"{stats['cpu_percent']}%",
                inline=True
            )
            embed.add_field(
                name="💾 Memory Usage",
                value=f"{stats['memory_percent']}% ({stats['memory_usage']/1024/1024:.2f}MB/{stats['memory_limit']/1024/1024:.2f}MB)",
                inline=True
            )
        
        if container_info.get('ssh_command'):
            embed.add_field(
                name="🔐 Direct SSH Access",
                value=f"```{container_info['ssh_command']}```",
                inline=False
            )
        
        view = View()
        if container.status == 'running':
            stop_button = Button(label="⏹️ Stop", style=discord.ButtonStyle.red)
            stop_button.callback = lambda i: manage_server(i, "stop", container_id)
            view.add_item(stop_button)
            
            restart_button = Button(label="🔄 Restart", style=discord.ButtonStyle.blurple)
            restart_button.callback = lambda i: manage_server(i, "restart", container_id)
            view.add_item(restart_button)
        else:
            start_button = Button(label="▶️ Start", style=discord.ButtonStyle.green)
            start_button.callback = lambda i: manage_server(i, "start", container_id)
            view.add_item(start_button)
        
        ssh_button = Button(label="🔑 Regen SSH", style=discord.ButtonStyle.gray)
        ssh_button.callback = lambda i: regen_ssh_command(i, container_id)
        view.add_item(ssh_button)
        
        remove_button = Button(label="🗑️ Remove", style=discord.ButtonStyle.red)
        remove_button.callback = lambda i: manage_server(i, "remove", container_id)
        view.add_item(remove_button)
        
        await interaction.followup.send(embed=embed, view=view)
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="🥺 Server Not Found",
            description="The premium server no longer exists.",
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)
        remove_from_database(container_id)
    except Exception as e:
        embed = discord.Embed(
            title="🥺 Error Getting Server Info",
            description=str(e),
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)

# Slash commands
@bot.tree.command(name="deploy", description="✨ Create a new premium server")
async def deploy(interaction: discord.Interaction):
    """Show the image selection GUI for deployment"""
    view = ImageSelectView(interaction.user.id)
    
    embed = discord.Embed(
        title="🚀 Deploy a Premium Server",
        description="✨ Select your premium OS from the dropdown below:",
        color=0x9370db
    )
    embed.set_footer(text="✨ You have 60 seconds to choose | Made with 💖")
    
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="start", description="▶️ Start your premium server")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def start(interaction: discord.Interaction, container_id: str):
    """Start a stopped premium server"""
    await manage_server(interaction, "start", container_id)

@bot.tree.command(name="stop", description="⏹️ Stop your premium server")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def stop(interaction: discord.Interaction, container_id: str):
    """Stop a running premium server"""
    await manage_server(interaction, "stop", container_id)

@bot.tree.command(name="restart", description="🔄 Restart your premium server")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def restart(interaction: discord.Interaction, container_id: str):
    """Restart a premium server"""
    await manage_server(interaction, "restart", container_id)

@bot.tree.command(name="remove", description="🗑️ Remove your premium server")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def remove(interaction: discord.Interaction, container_id: str):
    """Remove a premium server"""
    await manage_server(interaction, "remove", container_id)

@bot.tree.command(name="regen-ssh", description="🔑 Generate a new SSH session")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    """Regenerate SSH session credentials"""
    await regen_ssh_command(interaction, container_id)

@bot.tree.command(name="info", description="ℹ️ Get info about a premium server")
@app_commands.describe(container_id="The ID of your premium server (first 12 chars)")
async def info(interaction: discord.Interaction, container_id: str):
    """Get detailed information about a premium server"""
    await show_instance_info(interaction, container_id)

@bot.tree.command(name="list", description="📋 List all your premium servers")
async def list_instances(interaction: discord.Interaction):
    """List all premium servers owned by the user"""
    user = str(interaction.user.id)
    containers = get_user_containers(user)
    
    if not containers:
        embed = discord.Embed(
            title="🥺 No Premium Servers Found",
            description="You don't have any premium servers yet. Use `/deploy` to create one! 😊",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = discord.Embed(
        title="📋 Your Premium Servers",
        description=f"✨ You have {len(containers)}/{SERVER_LIMIT} premium servers",
        color=0x9370db
    )
    
    for container in containers:
        image_data = DOCKER_IMAGES.get(container['image'], {})
        status = container.get('status', 'unknown').capitalize()
        
        embed.add_field(
            name=f"✨ {image_data.get('display_name', 'Server')} ({container['container_id'][:12]})",
            value=f"📊 Status: {status}\n📅 Created: {datetime.datetime.fromisoformat(container['created_at']).strftime('%Y-%m-%d')}",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stats", description="📊 Get system resource statistics")
async def stats(interaction: discord.Interaction):
    """Show system resource usage"""
    await interaction.response.defer()
    
    try:
        # Get host system stats
        cpu_percent = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get Docker stats
        total_containers = len(client.containers.list(all=True))
        running_containers = len(client.containers.list())
        
        embed = discord.Embed(
            title="📊 System Statistics",
            color=0x9370db
        )
        embed.add_field(
            name="⚡ CPU Usage",
            value=f"{cpu_percent}%",
            inline=True
        )
        embed.add_field(
            name="💾 Memory Usage",
            value=f"{memory.percent}% ({memory.used/1024/1024:.0f}MB/{memory.total/1024/1024:.0f}MB)",
            inline=True
        )
        embed.add_field(
            name="💿 Disk Usage",
            value=f"{disk.percent}% ({disk.used/1024/1024:.0f}MB/{disk.total/1024/1024:.0f}MB)",
            inline=True
        )
        embed.add_field(
            name="🐳 Docker Servers",
            value=f"{running_containers}/{total_containers} running",
            inline=True
        )
        embed.set_footer(text="✨ Made with 💖")
        
        await interaction.followup.send(embed=embed)
    
    except Exception as e:
        embed = discord.Embed(
            title="🥺 Error Getting Statistics",
            description=str(e),
            color=0xff69b4
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="❓ Show help information")
async def help_command(interaction: discord.Interaction):
    """Show help message"""
    embed = discord.Embed(
        title="✨ Premium Server Manager Help",
        description="✨ Manage your premium Docker servers through Discord",
        color=0x9370db
    )
    
    embed.add_field(
        name="🚀 /deploy",
        value="✨ Create a new premium server with a beautiful interface",
        inline=False
    )
    embed.add_field(
        name="📋 /list",
        value="✨ List all your premium servers",
        inline=False
    )
    embed.add_field(
        name="ℹ️ /info <id>",
        value="✨ Get detailed information about a premium server",
        inline=False
    )
    embed.add_field(
        name="▶️ /start <id>",
        value="✨ Start a stopped premium server",
        inline=False
    )
    embed.add_field(
        name="⏹️ /stop <id>",
        value="✨ Stop a running premium server",
        inline=False
    )
    embed.add_field(
        name="🔄 /restart <id>",
        value="✨ Restart a premium server",
        inline=False
    )
    embed.add_field(
        name="🔑 /regen-ssh <id>",
        value="✨ Generate new SSH credentials",
        inline=False
    )
    embed.add_field(
        name="🗑️ /remove <id>",
        value="✨ Permanently remove a premium server",
        inline=False
    )
    embed.add_field(
        name="📊 /stats",
        value="✨ Show system resource usage",
        inline=False
    )
    embed.set_footer(text="✨ Made with 💖 | Enjoy your premium experience!")
    
    await interaction.response.send_message(embed=embed)

# Admin commands
@bot.tree.command(name="admin-list", description="[ADMIN] 🛠️ List all premium servers")
async def admin_list(interaction: discord.Interaction):
    """Admin command to list all premium servers"""
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🥺 Permission Denied",
            description="This command is for admins only. 😊",
            color=0xff69b4
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    data = load_database()
    total_instances = sum(len(containers) for containers in data.values())
    
    embed = discord.Embed(
        title="🛠️ All Premium Servers",
        description=f"✨ There are {total_instances} premium servers in total",
        color=0x9370db
    )
    
    for user_id, containers in data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            username = user.name if user else f"Unknown User ({user_id})"
        except:
            username = f"Unknown User ({user_id})"
        
        embed.add_field(
            name=f"👤 {username}",
            value=f"💎 {len(containers)} premium servers",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed)

# Error handling
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        embed = discord.Embed(
            title="🥺 Command Not Found",
            description="The command you tried to use doesn't exist. 😊",
            color=0xff69b4
        )
    else:
        embed = discord.Embed(
            title="🥺 An Error Occurred",
            description=f"```{str(error)}```",
            color=0xff69b4
        )
        logger.error(f"Command error: {error}")
    
    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except:
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except:
            pass

bot.run(TOKEN)
