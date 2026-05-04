import json
import os
import uuid
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from google import genai
from google.genai import types
from dotenv import load_dotenv

# load environment variables from .env file
load_dotenv()

# intialize flask application
app = Flask(__name__)

# configure the SQLlite database
# tells flask to create the .db file

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat_history.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# attach SQLAlchemy to FLask application
db = SQLAlchemy(app)

# Intialize the gemini api client
# automatically detects GEMINI_API_KEY from .env file
client = genai.Client()


# DATABASE MODELS

class Message (db.Model):
    """
    Defines schema for SQL database
    Structure allows for forking  by linking each message to its parent,
    Creating a tree.
    """
    # generate a random string UUID for each messageID
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # the actual text of response
    text = db.Column(db.Text, nullable=False)

    #sender(role || model)
    role = db.Column(db.String(10), nullable=False)

    # points to the message that came directly before it
    parent_id = db.Column(db.String(36), db.ForeignKey('message.id'), nullable=True)

# creates database tables if they don't exist yet
with app.app_context():
    db.create_all()


# CORE LOGIC

def get_active_thread(node_id):
    """
    gets a node_id and walks backwards through the chain via parent_id to create
    the chat history.
    """
    thread = []
    current_id = node_id

    while current_id:
        #look up the message in the database
        msg = db.session.get(Message, current_id)

        #if dead end, stop tracing
        if not msg:
            break

        # insert the message at the beginning of the array to perserve 
        # chronological order
        thread.insert(0, {"role":msg.role, "text":msg.text})
        
        # move up to next chat
        current_id = msg.parent_id 

    return thread

# API ENDPOINTS

@app.route('/chat', methods=['Post'])
def chat():
    """
    the main endpoint. recieves a prompt and parent_id, saves the users prompt,
    fetches the history, asks gemini for a response, and saves that response.
    """
    # parse incoming data from json frontend
    data = request.json
    if isinstance(data, str):
        data = json.loads(data)
    user_text = data.get('text')

    # gets parent_id (null if no parent_id)
    parent_id = data.get('parent_id')

    if not user_text:
        return jsonify({"error": "No text provided"}), 400
    
    # save the user's message ot the database
    user_msg = Message(text=user_text, role='user', parent_id=parent_id)
    db.session.add(user_msg)
    db.session.commit()

    # reconstruct history by calling get_active_thread function
    history_dicts = get_active_thread(parent_id) if parent_id else []

    # format the history into gemini SDK compatible structure
    formatted_contents = []
    for msg in history_dicts:
        formatted_contents.append(
            types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])])
        )

    # append the message just created to this list
    formatted_contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
    )

    # call gemini api
    try:
        #using flash model
        response=client.models.generate_content(
            model='gemini-3.1-flash-lite-preview',
            contents=formatted_contents
        )
        model_text=response.text
    except Exception as e:
        #if fails, let client know why
        return jsonify({"error": str(e)}), 500
    
    # put model into Message class structure then save to database
    model_msg = Message(text=model_text, role='model', parent_id=user_msg.id)
    db.session.add(model_msg)
    db.session.commit()

    # return the new state to the client
    #return both IDs so the frontend knows what to point to next
    return jsonify({
        "user_message_id": user_msg.id,
        "model_message_id": model_msg.id,
        "response": model_text
    })

# RUN THE APP

if __name__ == '__main__':
    #debug = True automatically restarts the server when you save changes
    app.run(debug=True)




