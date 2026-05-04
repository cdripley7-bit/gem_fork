import json
import os
import uuid
from flask import Flask, request, jsonify, render_template
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
            
        # Find all messages that share this exact same parent_id (the siblings/branches)
        if msg.parent_id is None:
            siblings = Message.query.filter_by(parent_id=None).order_by(Message.id).all()
        else:
            siblings = Message.query.filter_by(parent_id=msg.parent_id).order_by(Message.id).all()
            
        sibling_ids = [s.id for s in siblings]
        current_index = sibling_ids.index(msg.id)

        # insert the message at the beginning of the array to perserve 
        # chronological order
        # We now return the ID, parent_id, and the branch math!
        thread.insert(0, {
            "id": msg.id,
            "role": msg.role, 
            "text": msg.text,
            "parent_id": msg.parent_id,
            "siblings": sibling_ids,
            "branch_index": current_index + 1, 
            "total_branches": len(sibling_ids)
        })
        
        # move up to next chat
        current_id = msg.parent_id 

    return thread

# recursive deletion logic
def delete_node_and_children(node_id):
    """Recursively finds and deletes a message and all of its downstream replies."""
    children = Message.query.filter_by(parent_id=node_id).all()
    for child in children:
        delete_node_and_children(child.id)
        
    msg = db.session.get(Message, node_id)
    if msg:
        db.session.delete(msg)

#ROUTES

# route to wipe the entire DB
@app.route('/clear', methods=['DELETE'])
def clear_chat():
    """Wipes the entire database for a fresh session."""
    db.session.query(Message).delete()
    db.session.commit()
    return jsonify({"success": True})

# route to delete a specific branch
@app.route('/delete_branch/<node_id>', methods=['DELETE'])
def delete_branch(node_id):
    """Deletes a branch and smartly falls back to a sibling timeline."""
    msg = db.session.get(Message, node_id)
    if not msg:
        return jsonify({"error": "Message not found"}), 404
        
    parent_id = msg.parent_id
    
    # 1. before we delete, find a sibling branch to jump to
    if parent_id is None:
        siblings = Message.query.filter(Message.parent_id == None, Message.id != node_id).all()
    else:
        siblings = Message.query.filter(Message.parent_id == parent_id, Message.id != node_id).all()
        
    # If there are siblings, our fallback is the first one. Otherwise, fallback to the parent.
    fallback_node_id = siblings[0].id if siblings else parent_id
    
    # 2. Recursively delete the requested node and everything below it
    delete_node_and_children(node_id)
    db.session.commit()
    
    # 3. Walk down the tree from our fallback node so the screen doesn't go blank
    if fallback_node_id:
        current_id = fallback_node_id
        while True:
            child = Message.query.filter_by(parent_id=current_id).first()
            if not child:
                break
            current_id = child.id
            
        return jsonify({
            "thread": get_active_thread(current_id),
            "active_node_id": current_id
        })
    else:
        # If we deleted the very first message of the chat, return a blank slate
        return jsonify({"thread": [], "active_node_id": None})


@app.route('/')
def home():
    # This tells Flask to serve an HTML file when you visit the base URL
    return render_template('index.html')


# route to handle clicking the left/right branch arrows
@app.route('/load_branch/<node_id>', methods=['GET'])
def load_branch(node_id):
    """When a user clicks an arrow, we load that specific timeline."""
    current_node_id = node_id
    
    # Walk down the tree to find the very bottom of this specific timeline
    while True:
        # Find a child message that replies to our current node
        # (If a branch has multiple sub-branches, .first() will just follow the first path)
        child = Message.query.filter_by(parent_id=current_node_id).first()
        
        # If there are no more replies, we've hit the end of the chat timeline!
        if not child:
            break
            
        # Move down to the child and loop again
        current_node_id = child.id
            
    # Now we build the thread starting from the absolute bottom of the chat
    return jsonify({
        "thread": get_active_thread(current_node_id),
        "active_node_id": current_node_id
    })


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
    # NEW: Return the completely mapped thread array instead of just the IDs
    return jsonify({
        "thread": get_active_thread(model_msg.id),
        "active_node_id": model_msg.id
    })

# RUN THE APP

if __name__ == '__main__':
    #debug = True automatically restarts the server when you save changes
    app.run(debug=True)



