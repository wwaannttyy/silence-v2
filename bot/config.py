import yaml
import dotenv
from pathlib import Path

from bot.mixpanel_wrapper import MixpanelWrapper


config_dir = Path(__file__).parent.parent.resolve() / "config"

# load yaml config
with open(config_dir / "config.yml", 'r') as f:
    config_yaml = yaml.safe_load(f)

# load .env config
config_env = dotenv.dotenv_values(config_dir / "config.env")

# config parameters
telegram_token = config_yaml["telegram_token"]
openai_api_key = config_yaml["openai_api_key"]
allowed_telegram_usernames = config_yaml["allowed_telegram_usernames"]
new_dialog_timeout = config_yaml["new_dialog_timeout"]
enable_message_streaming = config_yaml.get("enable_message_streaming", True)
initial_token_balance = config_yaml["initial_token_balance"]
default_lang = config_yaml.get("default_lang", "en")
return_n_generated_images = config_yaml.get("return_n_generated_images", 1)
n_chat_modes_per_page = config_yaml.get("n_chat_modes_per_page", 7)
mongodb_uri = f"mongodb://mongo:{config_env['MONGODB_PORT']}"

# files
help_group_chat_video_path = Path(__file__).parent.parent.resolve() / "static" / "help_group_chat.mp4"

# strings
with open(config_dir / "strings.yml", 'r') as f:
    strings = yaml.safe_load(f)

# ref
enable_ref_system = config_yaml["enable_ref_system"]
n_tokens_to_add_to_ref = config_yaml["n_tokens_to_add_to_ref"]
max_invites_per_user = config_yaml["max_invites_per_user"]

# admin
support_username = config_yaml["support_username"]
bot_username = config_yaml["bot_username"]
bot_name = config_yaml["bot_name"]

admin_chat_id = config_yaml["admin_chat_id"]
admin_usernames = config_yaml["admin_usernames"]

# chat_modes
with open(config_dir / "chat_modes.yml", 'r') as f:
    chat_modes = yaml.safe_load(f)

# models
with open(config_dir / "models.yml", 'r') as f:
    models = yaml.safe_load(f)

# payments
payment_methods = config_yaml["payment_methods"]
products = config_yaml["products"]

# mixpanel
mxp = MixpanelWrapper(token=config_yaml["mixpanel_project_token"])
