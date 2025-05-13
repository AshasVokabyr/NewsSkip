from config import (
    SUPABASE_URL, SUPABASE_KEY
)
from typing import Optional, Dict, List
from supabase import create_client, Client
from supabase.client import ClientOptions
from supabase import PostgrestAPIError


class Database:
    """Класс для работы с сообщениями Telegram-бота в Supabase."""
    
    def __init__(self):
        """Инициализация подключения к Supabase."""
        supabase_url = SUPABASE_URL
        supabase_key = SUPABASE_KEY
        
        if not supabase_url or not supabase_key:
            raise ValueError("Supabase credentials not found in environment variables")
            
        self.client: Client = create_client(
            supabase_url,
            supabase_key,
            options=ClientOptions(postgrest_client_timeout=10)
        )
        self.table_name = "messages"
    
    async def insert_post(
        self,
        telegram_id: int,
        message_text: str,
        url: Optional[str] = None,  # Теперь принимает JSON-строку
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        parent_id: Optional[int] = None
    ) -> Optional[Dict]:
        try:
            post_data = {
                "telegram_id": telegram_id,
                "message_text": message_text,
                "url": url,
                "is_post": True,
                "user_id": user_id,
                "username": username,
                "parent_id": parent_id
            }
            
            # Удаляем None значения
            post_data = {k: v for k, v in post_data.items() if v is not None}
            
            response = (
                self.client
                .table(self.table_name)
                .insert(post_data)
                .execute()
            )
            
            if not response.data:
                return None
                
            return response.data[0]
            
        except PostgrestAPIError as e:  # Используем новое имя исключения
            print(f"Database error while inserting post: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error while inserting post: {e}")
            return None
    
    async def add_message(
        self,
        telegram_id: int,
        message_text: str,
        user_id: int,
        username: Optional[str] = None,
        url: Optional[str] = None,
        is_post: bool = False,
        parent_id: Optional[int] = None
    ) -> Optional[Dict]:
        """Добавляет новое сообщение в базу данных."""
        try:
            message_data = {
                "telegram_id": telegram_id,
                "message_text": message_text,
                "user_id": user_id,
                "is_post": is_post,
                "username": username,
                "url": url,
                "parent_id": parent_id
            }
            
            # Удаляем None значения
            message_data = {k: v for k, v in message_data.items() if v is not None}
            
            response = (
                self.client
                .table(self.table_name)
                .insert(message_data)
                .execute()
            )
            
            if not response.data:
                return None
                
            return response.data[0]
            
        except PostgrestAPIError as e:
            print(f"Database error: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error: {e}")
            return None
    
    async def get_message_by_id(self, id: int) -> Optional[Dict]:
        """Получает сообщение по его ID."""
        try:
            response = (
                self.client
                .table(self.table_name)
                .select("*")
                .eq("id", id)
                .execute()
            )
            return response.data[0] if response.data else None
        except PostgrestAPIError as e:
            print(f"Database error: {e}")
            return None
    
    async def get_replies_by_parent_id(self, parent_id: int) -> List[Dict]:
        """Получает все ответы на указанное сообщение."""
        try:
            response = (
                self.client
                .table(self.table_name)
                .select("*")
                .eq("parent_id", parent_id)
                .execute()
            )
            return response.data if response.data else []
        except PostgrestAPIError as e:
            print(f"Database error: {e}")
            return []
    
    async def update_message(self, id: int, fields: Dict) -> Optional[Dict]:
        """Обновляет данные сообщения."""
        try:
            # Удаляем None значения
            fields = {k: v for k, v in fields.items() if v is not None}
            
            if not fields:
                raise ValueError("No fields to update provided")
                
            response = (
                self.client
                .table(self.table_name)
                .update(fields)
                .eq("id", id)
                .execute()
            )
            return response.data[0] if response.data else None
        except PostgrestAPIError as e:
            print(f"Database error: {e}")
            return None
        except ValueError as e:
            print(f"Validation error: {e}")
            return None

    
