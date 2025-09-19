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
SERVER_LIMIT = 1  # Increased limit per user
DATABASE_FILE = 'database.json'  # Changed to JSON for better structure
LOG_FILE = 'bot.log'
ADMIN_IDS = [yourid]  # Add your admin user IDs here
ALLOWED_CHANNEL_ID = 92962972  # Only this channel can use commands

# Available Docker images with metadata
DOCKER_IMAGES = {
    "ubuntu-22.04": {
        "name": "ubuntu-22.04-with-tmate",
        "display_name": "Ubuntu 22.04 🌸",
        "description": "Adorable Ubuntu 22.04 with tmate pre-installed ✨",
        "ram": "6GB",
        "cpu": "2 cores"
    },
}

# Cute pastel color palette
COLORS = {
    'pink': 0xFFB3E6,      # Soft pink
    'purple': 0xC8A8E9,    # Lavender
    'blue': 0xB3D9FF,      # Baby blue
    'green': 0xB3FFB3,     # Mint green
    'yellow': 0xFFE0B3,    # Peach
    'red': 0xFFB3B3,       # Rose pink
    'success': 0xC8E6C9,   # Light green
    'error': 0xFFCDD2,     # Light red
    'info': 0xE1F5FE,      # Light blue
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

# Channel restriction check
def check_allowed_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == ALLOWED_CHANNEL_ID

class ImageSelectView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.selected_image = None
        
        # Create a dropdown for image selection
        select = Select(
            placeholder="Choose your perfect OS image~ 🌸",
            options=[
                discord.SelectOption(
                    label=img["display_name"],
                    description=img["description"],
                    value=img_name,
                    emoji="💖"
                ) for img_name, img in DOCKER_IMAGES.items()
            ]
        )
        select.callback = self.select_callback
        self.add_item(select)
        
        # Add deploy button
        deploy_button = Button(label="Deploy My Instance!", style=discord.ButtonStyle.green, emoji="✨")
        deploy_button.callback = self.deploy_callback
        self.add_item(deploy_button)
    
    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("💔 This isn't your deployment, sweetie!", ephemeral=True)
            return
            
        self.selected_image = interaction.data['values'][0]
        img_data = DOCKER_IMAGES[self.selected_image]
        
        embed = discord.Embed(
            title="🌟 Perfect Choice!",
            description=f"**{img_data['display_name']}** is ready to deploy~ 💖",
            color=COLORS['success']
        )
        embed.add_field(name="✨ Description", value=img_data["description"], inline=False)
        embed.add_field(name="🎀 Resources", value=f"{img_data['ram']} RAM | {img_data['cpu']} CPU", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def deploy_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("💔 This isn't your deployment, sweetie!", ephemeral=True)
            return
            
        if not self.selected_image:
            await interaction.response.send_message("🥺 Please select an image first, cutie!", ephemeral=True)
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

from typing import List, Dict

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
                container["user_id"] = user_id  # Add user_id to container info
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
    while True:
        output = await process.stdout.readline()
        if not output:
            break
        output = output.decode('utf-8').strip()
        if "ssh session:" in output:
            return output.split("ssh session:")[1].strip()
    return None

async def execute_command(command: str) -> tuple:
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return stdout.decode(), stderr.decode()

# Bot events
@bot.event
async def on_ready():
    change_status.start()
    logger.info(f'NXH-i7 Bot is ready. Logged in as {bot.user}')
    await bot.tree.sync()

@tasks.loop(seconds=30)
async def change_status():
    try:
        cute_statuses = [
            "💖 Creating magic with NXH-i7",
            "🌸 Building dreams in the cloud",
            "✨ Spreading cuteness everywhere",
            "🐱 Purring with happiness",
            "💫 Making instances extra special",
            "🎀 Dancing with containers"
        ]
        
        current_status = random.choice(cute_statuses)
        await bot.change_presence(activity=discord.Game(name=current_status))
    except Exception as e:
        logger.error(f"Failed to update status: {e}")

# Command functions
async def create_server_task(interaction: discord.Interaction, image_name: str):
    user = str(interaction.user.id)
    
    if count_user_containers(user) >= SERVER_LIMIT:
        embed = discord.Embed(
            title="🥺 Instance Limit Reached",
            description=f"You can only have {SERVER_LIMIT} adorable instances at a time, sweetie! 💔",
            color=COLORS['error']
        )
        embed.add_field(name="💡 Tip", value="Remove an existing instance to make room for a new one! 🌸", inline=False)
        await interaction.followup.send(embed=embed)
        return
    
    image_data = DOCKER_IMAGES.get(image_name)
    if not image_data:
        embed = discord.Embed(
            title="😿 Invalid Image",
            description="The selected image isn't available right now, cutie!",
            color=COLORS['error']
        )
        await interaction.followup.send(embed=embed)
        return
    
    # Send initial embed with loading animation
    embed = discord.Embed(
        title=f"✨ Creating Your {image_data['display_name']} Instance",
        description="Your magical instance is being prepared with love~ 💖",
        color=COLORS['info']
    )
    embed.add_field(name="🌟 Status", value="🔄 Sprinkling magic dust...", inline=False)
    message = await interaction.followup.send(embed=embed)
    
    try:
        # Step 1: Pull the image if not exists
        embed.set_field_at(0, name="🌟 Status", value="🔍 Checking for magical components...", inline=False)
        await message.edit(embed=embed)
        
        try:
            client.images.get(image_data['name'])
        except docker.errors.ImageNotFound:
            embed.set_field_at(0, name="🌟 Status", value="⬇️ Downloading cute components...", inline=False)
            await message.edit(embed=embed)
            
            try:
                client.images.pull(image_data['name'])
            except docker.errors.DockerException as e:
                logger.error(f"Error pulling image {image_data['name']}: {e}")
                raise Exception(f"Failed to download magical components: {e}")
        
        # Step 2: Create container
        embed.set_field_at(0, name="🌟 Status", value="🛠️ Assembling your instance with care...", inline=False)
        await message.edit(embed=embed)
        
        try:
            container = client.containers.run(
                image_data['name'],
                detach=True,
                tty=True,
                mem_limit='6g',  # 6GB memory limit
                cpu_quota=200000,  # Limit CPU usage
                cpu_shares=512,  # CPU priority
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
            )
            container_id = container.id
        except docker.errors.DockerException as e:
            logger.error(f"Error creating container: {e}")
            raise Exception(f"Failed to create your adorable instance: {e}")
        
        # Step 3: Start tmate session
        embed.set_field_at(0, name="🌟 Status", value="🔑 Creating secure access magic...", inline=False)
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
            container.stop()
            container.remove()
            raise Exception(f"Failed to create magical access: {e}")
        
        # Step 4: Finalize
        add_to_database(user, container_id, ssh_session_line, image_name)
        
        # Create success embed
        success_embed = discord.Embed(
            title=f"🎉 Your {image_data['display_name']} is Ready!",
            description="Your adorable instance has been created with lots of love! 💖",
            color=COLORS['success']
        )
        success_embed.add_field(
            name="🔐 SSH Access (Keep this secret!)",
            value=f"```{ssh_session_line}```",
            inline=False
        )
        success_embed.add_field(
            name="🎀 Resources",
            value=f"{image_data['ram']} RAM | {image_data['cpu']} CPU",
            inline=True
        )
        success_embed.add_field(
            name="🛠️ Management",
            value=f"Use `/stop {container_id[:12]}` to pause this cutie",
            inline=True
        )
        success_embed.add_field(
            name="💡 Pro Tip",
            value="Save your SSH command somewhere safe! 🌸",
            inline=False
        )
        
        # Send to user's DMs
        try:
            await interaction.user.send(embed=success_embed)
        except discord.Forbidden:
            logger.warning(f"Could not send DM to user {interaction.user.id}")
        
        # Update original message
        embed.title = f"✅ Deployment Complete!"
        embed.description = f"Your {image_data['display_name']} instance is running beautifully! 🌟"
        embed.set_field_at(0, name="🌟 Status", value="✔️ All done with love!", inline=False)
        embed.color = COLORS['success']
        embed.add_field(
            name="💌 Next Steps",
            value="Check your DMs for SSH access details! 💖",
            inline=False
        )
        await message.edit(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in deployment: {e}")
        
        error_embed = discord.Embed(
            title="😿 Deployment Failed",
            description=f"Something went wrong: {str(e)}",
            color=COLORS['error']
        )
        error_embed.add_field(
            name="💔 Status",
            value="Failed - Please try again later, sweetie",
            inline=False
        )
        error_embed.add_field(
            name="🤗 Don't worry!",
            value="These things happen sometimes. Try again in a moment! 💖",
            inline=False
        )
        
        await message.edit(embed=error_embed)

async def manage_server(interaction: discord.Interaction, action: str, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🔍 Instance Not Found",
            description="No adorable instance found with that ID, sweetie! 🥺",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🚫 Permission Denied",
            description="You don't have permission to manage this cute instance! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        
        action_emojis = {
            "start": "▶️",
            "stop": "⏹️", 
            "restart": "🔄",
            "remove": "🗑️"
        }
        
        action_messages = {
            "start": "started and is running beautifully",
            "stop": "stopped peacefully",
            "restart": "restarted with fresh energy",
            "remove": "removed with care"
        }
        
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
            title=f"{action_emojis[action]} Instance {status.capitalize()}!",
            description=f"Your instance `{container_id[:12]}` has been {action_messages[action]}! 💖",
            color=COLORS['success']
        )
        
        if action != "remove":
            stats = await get_container_stats(container_id)
            if stats:
                embed.add_field(
                    name="📊 Current Stats",
                    value=f"🧠 CPU: {stats['cpu_percent']}% | 💾 Memory: {stats['memory_percent']}%",
                    inline=False
                )
        else:
            embed.add_field(
                name="💫 Farewell",
                value="Your instance has been safely removed! Create a new one anytime~ 🌸",
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
                        title=f"🔑 Fresh SSH Access for {image_data.get('display_name', 'Your Instance')}",
                        description=f"Here's your new magical access key! 💖\n```{ssh_session_line}```",
                        color=COLORS['info']
                    )
                    dm_embed.add_field(
                        name="🆔 Instance ID",
                        value=container_id[:12],
                        inline=False
                    )
                    await interaction.user.send(embed=dm_embed)
            except Exception as e:
                logger.error(f"Error regenerating SSH session: {e}")
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="😿 Instance Not Found",
            description="The container no longer exists, sweetie!",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed)
        remove_from_database(container_id)
    except docker.errors.DockerException as e:
        embed = discord.Embed(
            title="💔 Error Managing Instance",
            description=f"Something went wrong: {str(e)}",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed)

async def regen_ssh_command(interaction: discord.Interaction, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🔍 Instance Not Found",
            description="No adorable instance found with that ID, sweetie! 🥺",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🚫 Permission Denied",
            description="You don't have permission to manage this cute instance! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        if container.status != 'running':
            raise Exception("Instance is not running right now")
        
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
            title=f"🔑 Fresh SSH Magic for {image_data.get('display_name', 'Your Instance')}",
            description=f"Here's your sparkly new access key! ✨\n```{ssh_session_line}```",
            color=COLORS['success']
        )
        embed.add_field(
            name="🆔 Instance ID",
            value=container_id[:12],
            inline=False
        )
        embed.add_field(
            name="💡 Keep it Safe!",
            value="Save this somewhere secure, cutie! 💖",
            inline=False
        )
        
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            embed=discord.Embed(
                title="✨ SSH Regenerated!",
                description="New magical access created! Check your DMs~ 💌",
                color=COLORS['success']
            )
        )
    
    except Exception as e:
        embed = discord.Embed(
            title="😿 Error Generating SSH",
            description=f"Something went wrong: {str(e)}",
            color=COLORS['error']
        )
        embed.add_field(
            name="💭 Suggestion",
            value="Make sure your instance is running first! 🌸",
            inline=False
        )
        await interaction.followup.send(embed=embed)

async def show_instance_info(interaction: discord.Interaction, container_id: str):
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="🔍 Instance Not Found",
            description="No adorable instance found with that ID, sweetie! 🥺",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    user = str(interaction.user.id)
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🚫 Permission Denied",
            description="You don't have permission to view this cute instance! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        stats = await get_container_stats(container_id)
        
        status_emojis = {
            'running': '💚',
            'stopped': '💤',
            'paused': '⏸️',
            'restarting': '🔄'
        }
        
        embed = discord.Embed(
            title=f"💖 {image_data.get('display_name', 'Your Adorable Instance')} Details",
            color=COLORS['info']
        )
        embed.add_field(
            name="🆔 Instance ID",
            value=f"`{container_id[:12]}`",
            inline=True
        )
        embed.add_field(
            name="🌟 Status",
            value=f"{status_emojis.get(container.status, '❓')} {container.status.capitalize()}",
            inline=True
        )
        embed.add_field(
            name="🎂 Birthday",
            value=datetime.datetime.fromisoformat(container_info['created_at']).strftime('%Y-%m-%d %H:%M'),
            inline=True
        )
        
        if stats:
            embed.add_field(
                name="🧠 CPU Usage",
                value=f"{stats['cpu_percent']}%",
                inline=True
            )
            embed.add_field(
                name="💾 Memory Usage",
                value=f"{stats['memory_percent']}%\n({stats['memory_usage']/1024/1024:.1f}MB/{stats['memory_limit']/1024/1024:.1f}MB)",
                inline=True
            )
            embed.add_field(
                name="💖 Health",
                value="Purring smoothly~ 🐱" if stats['online'] else "Taking a nap~ 😴",
                inline=True
            )
        
        if container_info.get('ssh_command'):
            embed.add_field(
                name="🔐 SSH Access",
                value=f"```{container_info['ssh_command']}```",
                inline=False
            )
        
        view = View()
        if container.status == 'running':
            stop_button = Button(label="Take a Nap", style=discord.ButtonStyle.secondary, emoji="💤")
            stop_button.callback = lambda i: manage_server(i, "stop", container_id)
            view.add_item(stop_button)
            
            restart_button = Button(label="Fresh Start", style=discord.ButtonStyle.primary, emoji="🔄")
            restart_button.callback = lambda i: manage_server(i, "restart", container_id)
            view.add_item(restart_button)
        else:
            start_button = Button(label="Wake Up!", style=discord.ButtonStyle.success, emoji="💚")
            start_button.callback = lambda i: manage_server(i, "start", container_id)
            view.add_item(start_button)
        
        ssh_button = Button(label="New SSH Magic", style=discord.ButtonStyle.secondary, emoji="🔑")
        ssh_button.callback = lambda i: regen_ssh_command(i, container_id)
        view.add_item(ssh_button)
        
        remove_button = Button(label="Say Goodbye", style=discord.ButtonStyle.danger, emoji="💔")
        remove_button.callback = lambda i: manage_server(i, "remove", container_id)
        view.add_item(remove_button)
        
        await interaction.followup.send(embed=embed, view=view)
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="😿 Instance Not Found",
            description="The container no longer exists, sweetie!",
            color=COLORS['error']
        )
        await interaction.followup.send(embed=embed)
        remove_from_database(container_id)
    except Exception as e:
        embed = discord.Embed(
            title="💔 Error Getting Info",
            description=f"Something went wrong: {str(e)}",
            color=COLORS['error']
        )
        await interaction.followup.send(embed=embed)

# Slash commands with channel restriction
@bot.tree.command(name="deploy", description="Create a new adorable instance! 💖")
async def deploy(interaction: discord.Interaction):
    """Show the image selection GUI for deployment"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
        
    view = ImageSelectView(interaction.user.id)
    
    embed = discord.Embed(
        title="✨ Welcome to NXH-i7! ✨",
        description="Let's create something magical together~ Choose your perfect OS image! 💖",
        color=COLORS['pink']
    )
    embed.add_field(
        name="🌸 What's NXH-i7?",
        value="Your cute cloud companion for creating adorable instances! 🐱",
        inline=False
    )
    embed.add_field(
        name="⏰ Time Limit",
        value="You have 60 seconds to choose, cutie!",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="start", description="Wake up your sleeping instance! 💚")
@app_commands.describe(container_id="The ID of your adorable instance (first 12 chars)")
async def start(interaction: discord.Interaction, container_id: str):
    """Start a stopped instance"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await manage_server(interaction, "start", container_id)

@bot.tree.command(name="stop", description="Put your instance to sleep~ 💤")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def stop(interaction: discord.Interaction, container_id: str):
    """Stop a running instance"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await manage_server(interaction, "stop", container_id)

@bot.tree.command(name="restart", description="Give your instance a fresh start! 🔄")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def restart(interaction: discord.Interaction, container_id: str):
    """Restart an instance"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await manage_server(interaction, "restart", container_id)

@bot.tree.command(name="remove", description="Say goodbye to your instance 💔")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def remove(interaction: discord.Interaction, container_id: str):
    """Remove an instance"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await manage_server(interaction, "remove", container_id)

@bot.tree.command(name="regen-ssh", description="Create fresh SSH magic! 🔑✨")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    """Regenerate SSH session credentials"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await regen_ssh_command(interaction, container_id)

@bot.tree.command(name="info", description="Get details about your cute instance! 💖")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def info(interaction: discord.Interaction, container_id: str):
    """Get detailed information about an instance"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await show_instance_info(interaction, container_id)

@bot.tree.command(name="list", description="See all your adorable instances! 🌸")
async def list_instances(interaction: discord.Interaction):
    """List all instances owned by the user"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
        
    user = str(interaction.user.id)
    containers = get_user_containers(user)
    
    if not containers:
        embed = discord.Embed(
            title="🥺 No Instances Found",
            description="You don't have any cute instances yet! Use `/deploy` to create your first one~ 💖",
            color=COLORS['yellow']
        )
        embed.add_field(
            name="✨ Getting Started",
            value="Type `/deploy` to begin your magical journey! 🌟",
            inline=False
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = discord.Embed(
        title="💖 Your Adorable Instance Collection",
        description=f"You have {len(containers)}/{SERVER_LIMIT} precious instances~ 🌸",
        color=COLORS['purple']
    )
    
    status_emojis = {
        'running': '💚',
        'stopped': '💤',
        'paused': '⏸️'
    }
    
    for container in containers:
        image_data = DOCKER_IMAGES.get(container['image'], {})
        status = container.get('status', 'unknown')
        status_emoji = status_emojis.get(status, '❓')
        
        embed.add_field(
            name=f"✨ {image_data.get('display_name', 'Cute Instance')}",
            value=f"🆔 `{container['container_id'][:12]}`\n{status_emoji} {status.capitalize()}\n🎂 {datetime.datetime.fromisoformat(container['created_at']).strftime('%Y-%m-%d')}",
            inline=True
        )
    
    embed.add_field(
        name="💡 Pro Tip",
        value="Use `/info <id>` to get detailed info about any instance! 🌟",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stats", description="See cute system statistics! 📊✨")
async def stats(interaction: discord.Interaction):
    """Show system resource usage"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
        
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
            title="📊 NXH-i7 System Statistics",
            description="Here's how our magical system is doing~ 💖",
            color=COLORS['blue']
        )
        embed.add_field(
            name="🧠 CPU Usage",
            value=f"{cpu_percent}%",
            inline=True
        )
        embed.add_field(
            name="💾 Memory Usage",
            value=f"{memory.percent}%\n({memory.used//1024//1024}MB/{memory.total//1024//1024}MB)",
            inline=True
        )
        embed.add_field(
            name="💿 Disk Usage",
            value=f"{disk.percent}%\n({disk.used//1024//1024}MB/{disk.total//1024//1024}MB)",
            inline=True
        )
        embed.add_field(
            name="🐳 Container Status",
            value=f"💚 {running_containers} active\n📦 {total_containers} total",
            inline=True
        )
        embed.add_field(
            name="🌟 System Health",
            value="Purring smoothly~ 🐱" if cpu_percent < 80 and memory.percent < 80 else "Working hard~ 💪",
            inline=True
        )
        embed.add_field(
            name="💖 Uptime Mood",
            value="Feeling fantastic! ✨",
            inline=True
        )
        
        await interaction.followup.send(embed=embed)
    
    except Exception as e:
        embed = discord.Embed(
            title="😿 Error Getting Statistics",
            description=f"Something went wrong: {str(e)}",
            color=COLORS['error']
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="Get help with NXH-i7! 🌸💡")
async def help_command(interaction: discord.Interaction):
    """Show help message"""
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
        
    embed = discord.Embed(
        title="💖 NXH-i7 Help Center 💖",
        description="Your guide to managing adorable cloud instances~ Let me show you all the magical commands! ✨",
        color=COLORS['pink']
    )
    
    embed.add_field(
        name="🚀 `/deploy`",
        value="Create a new adorable instance with our cute interface! 💖",
        inline=False
    )
    embed.add_field(
        name="📋 `/list`",
        value="See all your precious instances in one place~ 🌸",
        inline=False
    )
    embed.add_field(
        name="ℹ️ `/info <id>`",
        value="Get detailed info about any of your cute instances! 💡",
        inline=False
    )
    embed.add_field(
        name="💚 `/start <id>`",
        value="Wake up a sleeping instance~ Rise and shine! ☀️",
        inline=False
    )
    embed.add_field(
        name="💤 `/stop <id>`",
        value="Put an instance to sleep peacefully~ Sweet dreams! 🌙",
        inline=False
    )
    embed.add_field(
        name="🔄 `/restart <id>`",
        value="Give your instance a fresh start with new energy! ✨",
        inline=False
    )
    embed.add_field(
        name="🔑 `/regen-ssh <id>`",
        value="Create brand new SSH access magic~ Keep it secret! 🤫",
        inline=False
    )
    embed.add_field(
        name="💔 `/remove <id>`",
        value="Say goodbye to an instance (this is permanent!) 😢",
        inline=False
    )
    embed.add_field(
        name="📊 `/stats`",
        value="Check how our magical system is performing~ 💖",
        inline=False
    )
    
    embed.add_field(
        name="💡 Tips & Tricks",
        value="• Keep your SSH commands safe! 🔐\n• You can have up to 3 instances~ 🌸\n• DMs contain important info! 💌\n• Use short IDs (first 12 characters) 📝",
        inline=False
    )
    
    embed.add_field(
        name="🆘 Need More Help?",
        value="If something's not working, try again in a moment~ Our magic sometimes needs a second! ✨",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

# Admin commands
@bot.tree.command(name="admin-list", description="[ADMIN] View all instances in the system 👑")
async def admin_list(interaction: discord.Interaction):
    """Admin command to list all instances"""
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="🚫 Access Denied",
            description="This command is for admins only, cutie! 💖",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if not check_allowed_channel(interaction):
        embed = discord.Embed(
            title="🚫 Wrong Channel, sweetie!",
            description="This command can only be used in the designated NXH-i7 channel! 💔",
            color=COLORS['error']
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    data = load_database()
    total_instances = sum(len(containers) for containers in data.values())
    
    embed = discord.Embed(
        title="👑 Admin Panel - All Instances",
        description=f"Managing {total_instances} adorable instances across all users~ 💖",
        color=COLORS['purple']
    )
    
    for user_id, containers in data.items():
        if containers:  # Only show users with instances
            try:
                user = await bot.fetch_user(int(user_id))
                username = user.name if user else f"Unknown User ({user_id})"
            except:
                username = f"Unknown User ({user_id})"
            
            running_count = len([c for c in containers if c.get('status') == 'running'])
            
            embed.add_field(
                name=f"👤 {username}",
                value=f"💖 {len(containers)} instances\n💚 {running_count} running",
                inline=True
            )
    
    if not data:
        embed.add_field(
            name="🌸 So Peaceful!",
            value="No instances are currently active~ 😴",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)