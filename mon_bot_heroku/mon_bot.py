# -*- coding: utf-8 -*-
# Mon Bot Discord - Script Unifié et Complet

# ===================================================================================
# --- 1. IMPORTS
# ===================================================================================
import discord
from discord.ext import commands, tasks
from discord import ui
from datetime import datetime, timedelta, timezone, time
import asyncio
import re
import os
import logging
import json
from collections import defaultdict
import threading
import uuid

# Imports pour le serveur API
from flask import Flask, jsonify, abort

# Imports pour les APIs externes
from googleapiclient.discovery import build as google_api_build
from twitchAPI.twitch import Twitch
from twitchAPI.helper import first as twitch_first

# --- IMPORTATION FINALE DE VOTRE GESTIONNAIRE DE BASE DE DONNÉES ---
from database import DatabaseManager

# ===================================================================================
# --- 2. CONFIGURATION DU LOGGING
# ===================================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)-8s] %(name)s (%(lineno)d): %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger('discord_multiscrim_bot')

# ===================================================================================
# --- 3. CHARGEMENT DES VARIABLES D'ENVIRONNEMENT ET CONSTANTES
# ===================================================================================
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", 0))
ADMIN_PANEL_CHANNEL_ID = int(os.environ.get("ADMIN_PANEL_CHANNEL_ID", 0))
LINK_PANEL_CHANNEL_ID = int(os.environ.get("LINK_PANEL_CHANNEL_ID", 0))
RESULTS_CHANNEL_ID = int(os.environ.get("RESULTS_CHANNEL_ID", 0))
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

if not all([TOKEN, GUILD_ID, ADMIN_PANEL_CHANNEL_ID, LINK_PANEL_CHANNEL_ID, RESULTS_CHANNEL_ID]):
    logger.critical("ERREUR: Toutes les variables d'environnement (y compris RESULTS_CHANNEL_ID) doivent être définies.")
    exit()

MODE_CHANNELS = {
    "SOLO": {"announce_id": int(os.environ.get("SOLO_ANNOUNCE_ID", 0)), "emoji": "👤", "limit": 100},
    "DUO":  {"announce_id": int(os.environ.get("DUO_ANNOUNCE_ID", 0)), "emoji": "👥", "limit": 50},
    "TRIO": {"announce_id": int(os.environ.get("TRIO_ANNOUNCE_ID", 0)), "emoji": "👨‍👩‍👧", "limit": 33}
}
BLOCKED_DURATION_MINUTES = 10

# ===================================================================================
# --- 4. INITIALISATION DU BOT, API ET CACHES
# ===================================================================================
intents = discord.Intents.default()
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents, help_command=None)
bot.db_manager = DatabaseManager()
bot.youtube_api_client = None
bot.twitch_api_client = None

active_games = {}
message_reactions = {}

app = Flask(__name__)

# ===================================================================================
# --- 5. ROUTES DE L'API (POUR LE SITE WEB)
# ===================================================================================
def json_default_converter(o):
    if isinstance(o, (datetime, time)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

def to_json_response(data):
    return app.response_class(
        response=json.dumps(data, default=json_default_converter, indent=4),
        mimetype='application/json'
    )

@app.route('/api/games')
async def get_games():
    games_data = await bot.db_manager.get_all_games()
    return to_json_response(games_data)

@app.route('/api/games/<string:game_code>')
async def get_game_details_api(game_code):
    game_data = await bot.db_manager.get_game(game_code)
    if game_data: return to_json_response(game_data)
    return jsonify({"error": "Game not found"}), 404

@app.route('/api/games/<string:game_code>/participants')
async def get_game_participants_api(game_code):
    participants = await bot.db_manager.get_game_participants(game_code)
    return to_json_response(participants)

@app.route('/api/players')
async def get_players():
    players_data = await bot.db_manager.get_all_players()
    return to_json_response(players_data)

@app.route('/api/players/<int:player_id>')
async def get_player_details_api(player_id):
    player_data = await bot.db_manager.get_player(player_id)
    if player_data: return to_json_response(player_data)
    return jsonify({"error": "Player not found"}), 404

@app.route('/api/players/<int:player_id>/participations')
async def get_player_participations_api(player_id):
    participations = await bot.db_manager.get_player_participations(player_id)
    return to_json_response(participations)

@app.route('/api/players/<int:player_id>/sanction')
async def get_player_sanction_api(player_id):
    sanction = await bot.db_manager.get_active_sanction(player_id)
    return to_json_response({"active_sanction": sanction})

def run_flask_app():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# ===================================================================================
# --- 6. FONCTIONS UTILITAIRES ET DE VÉRIFICATION
# ===================================================================================
async def est_createur(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member): return False
    if member.guild_permissions.administrator: return True
    player_data = await bot.db_manager.get_player(member.id)
    return player_data and player_data.get('is_creator', False)

async def est_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

async def find_member(guild: discord.Guild, identifier: str) -> discord.Member | None:
    try:
        member_id = int(re.sub(r'[<@!>]', '', identifier))
        return guild.get_member(member_id)
    except (ValueError, TypeError):
        return discord.utils.get(guild.members, name=identifier.split('#')[0], discriminator=identifier.split('#')[1] if '#' in identifier else None)
        
async def obtenir_twitch_user_info(twitch_username: str) -> dict | None:
    if not bot.twitch_api_client or not isinstance(twitch_username, str): return None
    cleaned_username = twitch_username.split('/')[-1]
    try:
        user_info = await twitch_first(bot.twitch_api_client.get_users(logins=[cleaned_username]))
        if user_info:
            return {'id': user_info.id, 'login': user_info.login, 'display_name': user_info.display_name}
    except Exception: return None
    return None

async def get_initial_social_stats(player_id: int) -> str:
    player_data = await bot.db_manager.get_player(player_id)
    if player_data:
        return "Abos YT début: 124 (simulé)"
    return ""

# ===================================================================================
# --- 7. CLASSES D'INTERFACE UTILISATEUR (Modales & Vues)
# ===================================================================================
class LinkAccountModal(ui.Modal, title="Lier/Modifier Comptes"):
    epic_name_input = ui.TextInput(label="Pseudo Epic Games (Requis)", required=True)
    youtube_url_input = ui.TextInput(label="URL Chaîne YouTube (Optionnel)", required=False)
    twitch_username_input = ui.TextInput(label="Pseudo Twitch (Optionnel)", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        epic_name = self.epic_name_input.value.strip()
        youtube_url = self.youtube_url_input.value.strip() or None
        twitch_username = self.twitch_username_input.value.strip() or None

        if not epic_name:
            return await interaction.followup.send("⚠️ Le pseudo Epic Games est requis.", ephemeral=True)

        player_data = {'epic_name': epic_name, 'youtube_url': youtube_url}
        
        if twitch_username:
            twitch_info = await obtenir_twitch_user_info(twitch_username)
            if twitch_info:
                player_data.update({
                    'twitch_login': twitch_info['login'],
                    'twitch_display_name': twitch_info['display_name']
                })
            else:
                return await interaction.followup.send(f"❌ Pseudo Twitch '{twitch_username}' introuvable.", ephemeral=True)
        
        await bot.db_manager.upsert_player(interaction.user.id, player_data)
        
        embed = discord.Embed(title="✅ Comptes Mis à Jour", color=discord.Color.green())
        embed.add_field(name="Pseudo Epic", value=epic_name, inline=False)
        if youtube_url:
            embed.add_field(name="YouTube", value=f"[Lien]({youtube_url})", inline=False)
        if twitch_username and player_data.get('twitch_login'):
            embed.add_field(name="Twitch", value=f"[{player_data['twitch_display_name']}](https://twitch.tv/{player_data['twitch_login']})", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"Compte lié pour {interaction.user.name}: {player_data}")

class StartGameModal(ui.Modal, title="Lancer Nouvelle Partie"):
    game_name_input = ui.TextInput(label="Nom/Code de la partie", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await handle_start_game_logic(interaction, self.game_name_input.value)

class MemberIdentifierModal(ui.Modal):
    member_input = ui.TextInput(label="ID, Mention, ou Nom#Tag du Membre", required=True)
    def __init__(self, title: str, on_submit_logic):
        super().__init__(title=title)
        self.on_submit_logic = on_submit_logic
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.on_submit_logic(interaction, self.member_input.value)

class TerminateGameModal(ui.Modal, title="Terminer Partie et Désigner Gagnant"):
    game_code_input = ui.TextInput(label="Nom/Code exact de la partie", required=True)
    winner_input = ui.TextInput(label="ID, Mention, ou Nom#Tag du Gagnant", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await handle_end_game_logic(interaction, self.game_code_input.value, self.winner_input.value)

class LinkPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @ui.button(label="🔗 Lier/Modifier Comptes & Epic", style=discord.ButtonStyle.primary, custom_id="link_panel:open_modal")
    async def link_button_callback(self, interaction: discord.Interaction, button: ui.Button):
        existing_data = await bot.db_manager.get_player(interaction.user.id)
        modal = LinkAccountModal()
        if existing_data:
            modal.epic_name_input.default = existing_data.get('epic_name', '')
            modal.youtube_url_input.default = existing_data.get('youtube_url', '')
            modal.twitch_username_input.default = existing_data.get('twitch_login', '')
        await interaction.response.send_modal(modal)

class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data['custom_id']
        is_admin_button = custom_id in ["admin:auth_creator", "admin:revoke_creator", "admin:recreate_panel"]
        
        if is_admin_button:
            has_perm = await est_admin(interaction)
            perm_name = "Administrateur du serveur"
        else:
            has_perm = await est_createur(interaction)
            perm_name = "Créateur de partie"
            
        if not has_perm:
            await interaction.response.send_message(f"❌ Seuls les utilisateurs avec la permission '{perm_name}' peuvent utiliser ce bouton.", ephemeral=True)
        return has_perm
    
    @ui.button(label="Lancer Partie", style=discord.ButtonStyle.success, emoji="🚀", custom_id="admin:start_game", row=0)
    async def start_game(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(StartGameModal())
    @ui.button(label="Sanctionner", style=discord.ButtonStyle.danger, emoji="🔨", custom_id="admin:punish", row=0)
    async def punish_member(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(MemberIdentifierModal("Sanctionner un Membre", handle_punish_logic))
    @ui.button(label="Autoriser Créateur", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="admin:auth_creator", row=0)
    async def authorize_creator(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(MemberIdentifierModal("Autoriser Créateur", handle_authorize_creator_logic))
    @ui.button(label="Lever Sanction", style=discord.ButtonStyle.secondary, emoji="🕊️", custom_id="admin:unpunish", row=1)
    async def unpunish_member(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(MemberIdentifierModal("Lever Sanction", handle_unpunish_logic))
    @ui.button(label="Terminer Partie", style=discord.ButtonStyle.primary, emoji="🏆", custom_id="admin:end_game", row=1)
    async def end_game(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(TerminateGameModal())
    @ui.button(label="Retirer Créateur", style=discord.ButtonStyle.secondary, emoji="➖", custom_id="admin:revoke_creator", row=1)
    async def revoke_creator(self, interaction: discord.Interaction, button: ui.Button): await interaction.response.send_modal(MemberIdentifierModal("Retirer Autorisation", handle_revoke_creator_logic))
    @ui.button(label="Recréer Panel", style=discord.ButtonStyle.danger, emoji="♻️", custom_id="admin:recreate_panel", row=2)
    async def recreate_panel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        await send_or_recreate_admin_panel(interaction.channel)
        await interaction.followup.send("✅ Panneau recréé.", ephemeral=True)

# ===================================================================================
# --- 8. LOGIQUE DE GESTION (HANDLERS)
# ===================================================================================

async def handle_start_game_logic(interaction: discord.Interaction, game_code: str):
    game_code_processed = game_code.strip().lower()
    if not re.match(r"^[a-zA-Z0-9_.-]+$", game_code_processed):
        return await interaction.followup.send("⚠️ Le nom de la partie est invalide.", ephemeral=True)
    if game_code_processed in active_games:
        return await interaction.followup.send(f"❌ Une partie avec le code `{game_code_processed}` est déjà active.", ephemeral=True)

    mode_emojis = [d["emoji"] for d in MODE_CHANNELS.values() if d.get("announce_id", 0) > 0]
    if not mode_emojis:
        return await interaction.followup.send("❌ Aucun mode de jeu n'a de salon d'annonce configuré.", ephemeral=True)
        
    embed_select = discord.Embed(title=f"🚀 Création Partie: `{game_code_processed}`", description=f"{interaction.user.mention}, choisissez le mode:", color=discord.Color.purple())
    options_text = "\n".join([f"{d['emoji']} : **{m}**" for m, d in MODE_CHANNELS.items() if d.get("announce_id", 0) > 0])
    embed_select.add_field(name="Modes Disponibles", value=options_text)
    
    mode_select_msg = await interaction.channel.send(embed=embed_select, delete_after=60.0)
    for emoji in mode_emojis: await mode_select_msg.add_reaction(emoji)
    
    await interaction.followup.send(f"⏳ Veuillez choisir un mode pour la partie `{game_code_processed}` dans le message ci-dessus.", ephemeral=True)

    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=lambda r, u: u.id == interaction.user.id and r.message.id == mode_select_msg.id and str(r.emoji) in mode_emojis)
        sel_mode, sel_details = next(((m, d) for m, d in MODE_CHANNELS.items() if str(reaction.emoji) == d["emoji"]), (None, None))
    except asyncio.TimeoutError: 
        return

    if not sel_mode: return

    ann_ch = interaction.guild.get_channel(sel_details["announce_id"])
    if not ann_ch:
        logger.error(f"Salon d'annonce introuvable pour le mode {sel_mode} (ID: {sel_details['announce_id']})")
        return
    
    link_panel_ch = interaction.guild.get_channel(LINK_PANEL_CHANNEL_ID)

    embed_annonce = discord.Embed(title=f"Nouvelle Partie [{sel_mode}]: {game_code_processed}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed_annonce.add_field(name="Lancée par", value=interaction.user.mention, inline=False)
    embed_annonce.add_field(name="Comment participer ?", value=f"1. Réagissez avec ✅ pour rejoindre (Limite: {sel_details['limit']}).\n2. Liez vos comptes via {link_panel_ch.mention} !", inline=False)
    embed_annonce.add_field(name="Instructions Créateur", value=f"{interaction.user.mention} clique ▶️ pour démarrer (verrouiller les inscriptions), ou 🛑 pour annuler.", inline=False)
    embed_annonce.set_footer(text=f"Limite totale joueurs: {sel_details['limit']}")
    
    ann_msg = await ann_ch.send(embed=embed_annonce)
    for emoji in ["✅", "▶️", "🛑"]: await ann_msg.add_reaction(emoji)

    game_data = {'game_code': game_code_processed, 'mode': sel_mode, 'creator_id': interaction.user.id, 'announce_message_id': ann_msg.id, 'announce_channel_id': ann_ch.id, 'status': 'pending', 'limit': sel_details['limit']}
    await bot.db_manager.create_game(**game_data)
    active_games[game_code_processed] = game_data
    message_reactions[ann_msg.id] = game_code_processed
    logger.info(f"Partie '{game_code_processed}' créée par {interaction.user.name}.")

async def handle_end_game_logic(interaction: discord.Interaction, game_code: str, winner_identifier: str):
    game_code = game_code.strip().lower()
    game_data = active_games.get(game_code)
    if not game_data:
        return await interaction.followup.send(f"❌ La partie `{game_code}` n'est pas active.", ephemeral=True)

    winner = await find_member(interaction.guild, winner_identifier)
    winner_message = f"Gagnant non trouvé ({winner_identifier})"
    winner_names_for_db = []
    if winner:
        winner_data = await bot.db_manager.get_player(winner.id)
        yt_link = ""
        if winner_data and winner_data.get('youtube_url'):
            yt_link = f" ([YouTube]({winner_data['youtube_url']}))"
        epic_name = winner_data.get('epic_name', 'N/A') if winner_data else 'N/A'
        winner_message = f"{winner.mention}{yt_link} - Epic: {epic_name}"
        winner_names_for_db.append(epic_name)
    
    results_channel = interaction.guild.get_channel(RESULTS_CHANNEL_ID)
    if results_channel:
        victory_embed = discord.Embed(title=f"🏆 Victoire Partie {game_code} [{game_data['mode']}] !", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
        victory_embed.description = f"Félicitations à l'équipe gagnante :\n{winner_message}"
        await results_channel.send(embed=victory_embed)
    else:
        logger.error(f"Salon des résultats (ID: {RESULTS_CHANNEL_ID}) introuvable.")
    
    try:
        ann_ch = interaction.guild.get_channel(int(game_data['announce_channel_id']))
        ann_msg = await ann_ch.fetch_message(int(game_data['announce_message_id']))
        await ann_msg.delete()
    except Exception as e:
        logger.error(f"Impossible de supprimer le message d'annonce pour {game_code}: {e}")
        
    await bot.db_manager.update_game_status(game_code, 'finished', winner_names=winner_names_for_db)
    active_games.pop(game_code, None)
    if msg_id := game_data.get('announce_message_id'):
        message_reactions.pop(int(msg_id), None)
        
    await interaction.followup.send(f"✅ La partie `{game_code}` est terminée et le résultat a été annoncé.", ephemeral=True)

async def handle_punish_logic(interaction: discord.Interaction, member_identifier: str):
    target = await find_member(interaction.guild, member_identifier)
    if not target:
        return await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)

    if target.id == interaction.user.id:
        return await interaction.followup.send("❌ Vous ne pouvez pas vous sanctionner vous-même.", ephemeral=True)
    if target.guild_permissions.administrator and not await est_admin(interaction):
        return await interaction.followup.send("❌ Vous ne pouvez pas sanctionner un administrateur.", ephemeral=True)

    roles_to_save = [{'id': r.id, 'name': r.name} for r in target.roles if not r.is_default() and not r.is_premium_subscriber() and not r.managed and target.guild.me.top_role > r]
    end_time = datetime.now(timezone.utc) + timedelta(minutes=BLOCKED_DURATION_MINUTES)
    await bot.db_manager.add_sanction(target.id, end_time, json.dumps(roles_to_save))

    try:
        roles_to_remove = [r for r in target.roles if r.id in [role['id'] for role in roles_to_save]]
        if roles_to_remove:
            await target.remove_roles(*roles_to_remove, reason="Sanction via panel admin")
    except Exception as e:
        logger.error(f"Erreur en retirant les rôles de {target.name}: {e}")

    await interaction.followup.send(f"🔨 {target.mention} a été sanctionné pour {BLOCKED_DURATION_MINUTES} minutes.", ephemeral=True)
    
async def handle_unpunish_logic(interaction: discord.Interaction, member_identifier: str):
    target = await find_member(interaction.guild, member_identifier)
    if not target:
        return await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
    
    sanction = await bot.db_manager.get_active_sanction(target.id)
    if not sanction:
        return await interaction.followup.send(f"ℹ️ {target.mention} n'a pas de sanction active.", ephemeral=True)

    await bot.db_manager.remove_sanction(sanction['id'])
    roles_json = sanction.get('roles_json', '[]')
    roles_data = json.loads(roles_json)
    roles_to_restore = [interaction.guild.get_role(r['id']) for r in roles_data if interaction.guild.get_role(r['id'])]
    
    try:
        if roles_to_restore:
            await target.add_roles(*roles_to_restore, reason="Levée de sanction via panel admin")
    except Exception as e:
        logger.error(f"Erreur en restaurant les rôles de {target.name}: {e}")

    await interaction.followup.send(f"🕊️ La sanction de {target.mention} a été levée.", ephemeral=True)

async def handle_authorize_creator_logic(interaction: discord.Interaction, member_identifier: str):
    target = await find_member(interaction.guild, member_identifier)
    if not target or target.bot:
        return await interaction.followup.send("❌ Membre invalide ou bot.", ephemeral=True)
    await bot.db_manager.upsert_player(target.id, {'is_creator': True})
    await interaction.followup.send(f"✅ {target.mention} est maintenant un créateur de parties.", ephemeral=True)

async def handle_revoke_creator_logic(interaction: discord.Interaction, member_identifier: str):
    target = await find_member(interaction.guild, member_identifier)
    if not target:
        return await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
    await bot.db_manager.upsert_player(target.id, {'is_creator': False})
    await interaction.followup.send(f"➖ {target.mention} n'est plus un créateur de parties.", ephemeral=True)

# ===================================================================================
# --- 9. FONCTIONS DE DÉMARRAGE ET DE MAINTENANCE
# ===================================================================================
async def send_or_recreate_admin_panel(channel: discord.TextChannel):
    try:
        await channel.purge(limit=20, check=lambda m: m.author == bot.user)
        embed = discord.Embed(title="🛠️ Panneau Administrateur", description="Actions rapides pour les créateurs de parties.", color=discord.Color.dark_red())
        await channel.send(embed=embed, view=AdminPanelView())
        logger.info(f"Panneau Admin recréé dans #{channel.name}.")
    except Exception as e:
        logger.error(f"Erreur lors de la recréation du Panneau Admin : {e}")
        
async def send_or_recreate_link_panel(channel: discord.TextChannel):
    try:
        await channel.purge(limit=20, check=lambda m: m.author == bot.user)
        embed = discord.Embed(title="🔗 Liaison Comptes & Epic", description="Cliquez pour lier/modifier vos comptes (Epic, YouTube, Twitch).\n**Obligatoire pour participer.**", color=discord.Color.blurple())
        await channel.send(embed=embed, view=LinkPanelView())
        logger.info(f"Panneau de liaison recréé dans #{channel.name}.")
    except Exception as e:
        logger.error(f"Erreur lors de la recréation du Panneau de liaison : {e}")

async def load_persistent_views():
    bot.add_view(LinkPanelView())
    bot.add_view(AdminPanelView())
    logger.info("🔄 Vues persistantes enregistrées.")
    
# ===================================================================================
# --- 10. ÉVÉNEMENTS DU BOT (Events)
# ===================================================================================
@bot.event
async def on_ready():
    logger.info("-" * 40)
    logger.info(f"🚀 Bot '{bot.user.name}' est PRÊT !")
    target_guild = bot.get_guild(GUILD_ID)
    if not target_guild:
        return logger.critical(f"CRITIQUE: Serveur (GUILD ID: {GUILD_ID}) non trouvé !")
    
    # Initialisation des clients API
    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        try:
            bot.twitch_api_client = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, authenticate_app=True)
            logger.info("Client API Twitch initialisé.")
        except Exception as e:
            logger.error(f"Erreur d'initialisation de l'API Twitch: {e}")

    await bot.db_manager.connect()
    await load_persistent_views()
    
    admin_channel = target_guild.get_channel(ADMIN_PANEL_CHANNEL_ID)
    if admin_channel:
        logger.info("Vérification et recréation du panneau d'administration...")
        await send_or_recreate_admin_panel(admin_channel)
    else:
        logger.warning(f"Le canal du panneau admin (ID: {ADMIN_PANEL_CHANNEL_ID}) est introuvable.")
        
    link_channel = target_guild.get_channel(LINK_PANEL_CHANNEL_ID)
    if link_channel:
        logger.info("Vérification et recréation du panneau de liaison...")
        await send_or_recreate_link_panel(link_channel)
    else:
        logger.warning(f"Le canal du panneau de liaison (ID: {LINK_PANEL_CHANNEL_ID}) est introuvable.")
        
    logger.info(f"✅ Connecté au serveur: '{target_guild.name}'")
    logger.info("-" * 40)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id or not payload.guild_id: return
    
    game_code = message_reactions.get(payload.message_id)
    if not game_code: return

    game_data = active_games.get(game_code)
    if not game_data: return
    
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    if not member: return
    
    try:
        ann_ch = guild.get_channel(game_data['announce_channel_id'])
        ann_msg = await ann_ch.fetch_message(payload.message_id)
    except (discord.NotFound, KeyError):
        return

    # Logique pour le créateur
    if payload.user_id == game_data['creator_id']:
        if str(payload.emoji) == '🛑':
            await ann_msg.delete()
            active_games.pop(game_code, None)
            message_reactions.pop(payload.message_id, None)
            await bot.db_manager.update_game_status(game_code, 'cancelled')
            logger.info(f"Partie '{game_code}' annulée par le créateur.")
            return

        if str(payload.emoji) == '▶️':
            game_data['status'] = 'locked'
            await bot.db_manager.update_game_status(game_code, 'locked')
            embed = ann_msg.embeds[0]
            embed.color = discord.Color.red()
            embed.set_field_at(1, name="Inscriptions fermées !", value="La partie va bientôt commencer.", inline=False)
            await ann_msg.edit(embed=embed)
            logger.info(f"Partie '{game_code}' verrouillée par le créateur.")
            return

    # Logique pour les joueurs
    if str(payload.emoji) == '✅':
        player_data = await bot.db_manager.get_player(member.id)
        if not player_data or not player_data.get('epic_name'):
            link_ch = guild.get_channel(LINK_PANEL_CHANNEL_ID)
            try:
                await member.send(f"⚠️ Pour pouvoir rejoindre la partie `{game_code}`, vous devez d'abord lier votre compte Epic via {link_ch.mention} !")
            except discord.Forbidden:
                pass # L'utilisateur a ses MP fermés, on ne peut rien faire de plus.
            await ann_msg.remove_reaction(payload.emoji, member)
            return
            
        participants = await bot.db_manager.get_game_participants(game_code)
        if any(p['user_id'] == member.id for p in participants): # Vérifie si le joueur est déjà inscrit
            return # Ne rien faire s'il est déjà dans la liste
            
        if game_data.get('status') == 'locked':
            try: await member.send(f"Les inscriptions pour la partie `{game_code}` sont fermées.")
            except discord.Forbidden: pass
            await ann_msg.remove_reaction(payload.emoji, member)
            return

        if len(participants) >= game_data.get('limit', 999):
            try: await member.send(f"La partie `{game_code}` est complète.")
            except discord.Forbidden: pass
            await ann_msg.remove_reaction(payload.emoji, member)
            return
            
        await bot.db_manager.add_participant(game_code, member.id)
        
        # Confirmation publique et privée
        await ann_ch.send(f"👍 {member.mention} a rejoint `{game_code}` [{game_data['mode']}] !", delete_after=10)
        try:
            stats = await get_initial_social_stats(member.id)
            await member.send(f"✅ Vous avez rejoint la partie `{game_code}` [{game_data['mode']}] ! {stats}")
        except discord.Forbidden:
            pass # L'utilisateur a ses MP fermés

# ===================================================================================
# --- 11. BLOC DE LANCEMENT PRINCIPAL (AVEC SERVEUR API)
# ===================================================================================
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask_app)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("Serveur API démarré...")
    
    try:
        logger.info("Lancement du bot...")
        bot.run(TOKEN, log_handler=None)
    except Exception as e:
        logger.critical(f"\n❌ ERREUR FATALE AU LANCEMENT: {e}", exc_info=True)
    finally:
        logger.info("--- Arrêt du script du bot ---")