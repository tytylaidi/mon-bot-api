# -*- coding: utf-8 -*-
# Gestionnaire de Base de Données pour Mon Bot Discord

import os
import psycopg2
import psycopg2.extras
import json
import uuid
from datetime import datetime, timezone
import logging
import asyncio

class DatabaseManager:
    def __init__(self):
        # Récupération des identifiants depuis les variables d'environnement
        self.db_host = os.environ.get("SUPABASE_DB_HOST")
        self.db_port = os.environ.get("SUPABASE_DB_PORT")
        self.db_name = os.environ.get("SUPABASE_DB_NAME")
        self.db_user = os.environ.get("SUPABASE_DB_USER")
        self.db_password = os.environ.get("SUPABASE_DB_PASSWORD")
        self.conn = None
        self.logger = logging.getLogger('database_manager')
        if not self.logger.handlers:
            logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)-8s] %(name)s (%(lineno)d): %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    async def connect(self):
        """Établit et maintient la connexion à la base de données."""
        if self.conn and not self.conn.closed:
            try:
                await asyncio.to_thread(self.conn.cursor().execute, "SELECT 1")
                return True
            except psycopg2.Error:
                self.logger.warning("Connexion existante non valide, tentative de reconnexion.")
                self.conn = None
        
        if not all([self.db_host, self.db_port, self.db_name, self.db_user, self.db_password]):
            self.logger.critical("Identifiants de base de données manquants.")
            return False

        try:
            self.conn = await asyncio.to_thread(
                psycopg2.connect, host=self.db_host, port=self.db_port, database=self.db_name,
                user=self.db_user, password=self.db_password, options="-c search_path=public"
            )
            self.conn.autocommit = False
            self.logger.info(f"Connexion à la base de données '{self.db_name}' établie.")
            await self.create_tables()
            return True
        except Exception as e:
            self.logger.critical(f"Erreur de connexion à la base de données: {e}")
            self.conn = None
            return False

    async def _execute_query(self, query: str, params: tuple = None, fetch_one: bool = False, fetch_all: bool = False):
        """Exécute une requête SQL de manière sécurisée."""
        if not await self.connect():
            return None if fetch_one else [] if fetch_all else False
        try:
            with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                await asyncio.to_thread(cur.execute, query, params)
                if fetch_one: return await asyncio.to_thread(cur.fetchone)
                if fetch_all: return await asyncio.to_thread(cur.fetchall)
                await asyncio.to_thread(self.conn.commit)
                return True
        except Exception as e:
            self.logger.error(f"Erreur DB: {e}")
            if self.conn: await asyncio.to_thread(self.conn.rollback)
            return None if fetch_one else [] if fetch_all else False

    async def create_tables(self):
        """Vérifie et crée les tables nécessaires au démarrage."""
        self.logger.info("Vérification/création des tables...")
        
        # Table des joueurs
        await self._execute_query("""
            CREATE TABLE IF NOT EXISTS players (
                discord_id BIGINT PRIMARY KEY, epic_name TEXT, youtube_url TEXT,
                yt_channel_id TEXT, twitch_username TEXT, twitch_user_id TEXT,
                twitch_login TEXT, twitch_display_name TEXT, discord_name_at_link TEXT,
                is_creator BOOLEAN DEFAULT FALSE, game_count INTEGER DEFAULT 0,
                total_wins INTEGER DEFAULT 0, created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # Table des parties
        await self._execute_query("""
            CREATE TABLE IF NOT EXISTS games (
                game_code TEXT PRIMARY KEY, creator_id BIGINT NOT NULL, mode TEXT NOT NULL,
                announce_message_id BIGINT, announce_channel_id BIGINT, status TEXT NOT NULL, 
                limit INTEGER, winner_epic_names JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                end_time TIMESTAMP WITH TIME ZONE
            )
        """)
        
        # Table de participation (lien entre joueurs et parties)
        await self._execute_query("""
            CREATE TABLE IF NOT EXISTS game_participants (
                id SERIAL PRIMARY KEY, game_code TEXT NOT NULL, user_id BIGINT NOT NULL, 
                has_won_game BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (game_code) REFERENCES games(game_code) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES players(discord_id) ON DELETE CASCADE,
                UNIQUE (game_code, user_id)
            )
        """)

        # Table des sanctions
        await self._execute_query("""
            CREATE TABLE IF NOT EXISTS sanctions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id BIGINT NOT NULL,
                sanction_type TEXT NOT NULL, end_time TIMESTAMP WITH TIME ZONE NOT NULL, 
                roles_json JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        self.logger.info("Toutes les tables ont été vérifiées/créées.")

    # --- MÉTHODES POUR LES JOUEURS ---
    async def get_player(self, discord_id: int) -> dict | None:
        player = await self._execute_query("SELECT * FROM players WHERE discord_id = %s", (discord_id,), fetch_one=True)
        return dict(player) if player else None

    async def get_all_players(self) -> list[dict]:
        players = await self._execute_query("SELECT * FROM players", fetch_all=True)
        return [dict(p) for p in players] if players else []

    async def get_player_participations(self, player_id: int) -> list[dict]:
        query = "SELECT gp.game_code, gp.has_won_game, g.created_at, g.mode FROM game_participants gp JOIN games g ON gp.game_code = g.game_code WHERE gp.user_id = %s ORDER BY g.created_at DESC"
        participations = await self._execute_query(query, (player_id,), fetch_all=True)
        return [dict(p) for p in participations] if participations else []
        
    async def upsert_player(self, discord_id: int, data: dict):
        columns = list(data.keys())
        update_set_clause = ", ".join([f"{col} = EXCLUDED.{col}" for col in columns if col != 'discord_id'])
        query = f"INSERT INTO players (discord_id, {', '.join(columns)}) VALUES (%s, {', '.join(['%s'] * len(columns))}) ON CONFLICT (discord_id) DO UPDATE SET {update_set_clause}, updated_at = NOW()"
        params = (discord_id, *data.values())
        await self._execute_query(query, params)

    # --- MÉTHODES POUR LES PARTIES ---
    async def get_game(self, game_code: str) -> dict | None:
        game = await self._execute_query("SELECT * FROM games WHERE game_code = %s", (game_code,), fetch_one=True)
        return dict(game) if game else None
        
    async def get_all_games(self) -> list[dict]:
        games = await self._execute_query("SELECT * FROM games ORDER BY created_at DESC", fetch_all=True)
        return [dict(g) for g in games] if games else []
        
    async def get_active_games(self) -> list[dict]:
        games = await self._execute_query("SELECT * FROM games WHERE status IN ('pending', 'locked') ORDER BY created_at DESC", fetch_all=True)
        return [dict(g) for g in games] if games else []

    async def create_game(self, **data):
        columns = list(data.keys())
        query = f"INSERT INTO games ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
        params = tuple(data.values())
        await self._execute_query(query, params)
        
    async def update_game_status(self, game_code: str, status: str, winner_names: list = None):
        if status == 'finished':
            query = "UPDATE games SET status = %s, winner_epic_names = %s, end_time = NOW() WHERE game_code = %s"
            params = (status, json.dumps(winner_names), game_code)
        else:
            query = "UPDATE games SET status = %s, updated_at = NOW() WHERE game_code = %s"
            params = (status, game_code)
        await self._execute_query(query, params)

    # --- MÉTHODES POUR LES PARTICIPANTS ---
    async def add_participant(self, game_code: str, user_id: int):
        await self._execute_query("INSERT INTO game_participants (game_code, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (game_code, user_id))
    
    async def get_game_participants(self, game_code: str) -> list[dict]:
        query = """
            SELECT p.discord_id, p.epic_name, gp.has_won_game
            FROM game_participants gp
            JOIN players p ON gp.user_id = p.discord_id
            WHERE gp.game_code = %s
        """
        participants = await self._execute_query(query, (game_code,), fetch_all=True)
        return [dict(p) for p in participants] if participants else []

    # --- MÉTHODES POUR LES SANCTIONS ---
    async def add_sanction(self, user_id: int, end_time: datetime, roles_json: str, sanction_type: str = "manual"):
        await self._execute_query("INSERT INTO sanctions (user_id, sanction_type, end_time, roles_json) VALUES (%s, %s, %s, %s)", (user_id, sanction_type, end_time, roles_json))

    async def get_active_sanction(self, user_id: int) -> dict | None:
        sanction = await self._execute_query("SELECT * FROM sanctions WHERE user_id = %s AND end_time > NOW() LIMIT 1", (user_id,), fetch_one=True)
        return dict(sanction) if sanction else None

    async def remove_sanction(self, sanction_id: uuid.UUID):
        await self._execute_query("DELETE FROM sanctions WHERE id = %s", (sanction_id,))