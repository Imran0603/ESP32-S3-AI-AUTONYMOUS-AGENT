const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const path = require('path');
const cors = require('cors');
const { OpenAI } = require('openai');
const { GoogleGenerativeAI } = require('@google/generative-ai');
const Anthropic = require('@anthropic-ai/sdk');

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
let aiConfig = { provider: 'openai', apiKey: '', autopilot: false };
let lastScreenshot = null;
let isAIBusy = false;

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
      socket.emit('sync_ai_config', aiConfig);
    }
  });

  // AI Config Update
  socket.on('set_ai_config', (config) => {
    aiConfig = { ...aiConfig, ...config };
    io.emit('sync_ai_config', aiConfig);
  });

  // Autopilot Update
  socket.on('toggle_autopilot', (enabled) => {
    aiConfig.autopilot = enabled;
    io.emit('sync_ai_config', aiConfig);
    if (enabled && lastScreenshot && !isAIBusy) {
      processAIScreenshot(lastScreenshot);
    }
  });

  // 2. Receiving Screenshot from Agent
  socket.on('screenshot_data', (data) => {
    // data should be a base64 encoded image string
    lastScreenshot = data;
    // Forward the screenshot to all connected web dashboards
    io.emit('new_screenshot', data);
    
    if (aiConfig.autopilot && !isAIBusy) {
       processAIScreenshot(data);
    }
  });

  // 3. Receiving Heartbeat from Agent (Mod B)
  socket.on('heartbeat', () => {
    // console.log(`[HEARTBEAT] Received from agent`);
    // We could forward this to the dashboard if we want a visual indicator
    io.emit('heartbeat_received', Date.now());
  });

  // 4. Web Dashboard sends a command to the Agent
  socket.on('send_command', async (cmdString) => {
    if (aiConfig.apiKey && lastScreenshot) {
      console.log(`[AI] User requested manual AI action: ${cmdString}`);
      io.emit('error_msg', `Asking AI: ${cmdString}...`);
      await processAIScreenshot(lastScreenshot, cmdString);
    } else {
      if (agentConnected && agentSocketId) {
        console.log(`[COMMAND] Routing raw command to Agent: ${cmdString}`);
        io.to(agentSocketId).emit('execute_command', cmdString);
      } else {
        socket.emit('error_msg', 'Agent is offline. Command not sent.');
      }
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

// AI Logic
async function processAIScreenshot(imageBase64, customPrompt = null) {
  if (!aiConfig.apiKey) return;
  isAIBusy = true;
  try {
    let result = null;
    const promptText = customPrompt || "Analyze this screen and determine the next best action. If interacting with a web browser, use PinchTab commands like: {\"action\": \"pinchtab_navigate\", \"url\": \"https://...\"} or {\"action\": \"pinchtab_click\", \"selector\": \"#login-btn\"} or {\"action\": \"pinchtab_type\", \"selector\": \"#username\", \"text\": \"admin\"}. Otherwise, for general desktop use: {\"action\": \"move\"|\"click\"|\"type\"|\"press\"|\"nothing\", \"x\": integer, \"y\": integer, \"text\": \"string for type\", \"key\": \"enter/esc/etc\"}. Respond in STRICT JSON format only.";

    if (aiConfig.provider === 'openai') {
      const openai = new OpenAI({ apiKey: aiConfig.apiKey });
      const response = await openai.chat.completions.create({
        model: "gpt-4o",
        messages: [
          { role: "system", content: "You are an autonomous computer control agent. Respond only with valid JSON." },
          { role: "user", content: [
            { type: "text", text: promptText },
            { type: "image_url", image_url: { url: `data:image/jpeg;base64,${imageBase64}` } }
          ]}
        ],
        response_format: { type: "json_object" }
      });
      result = response.choices[0].message.content;
    } else if (aiConfig.provider === 'gemini') {
      const genAI = new GoogleGenerativeAI(aiConfig.apiKey);
      const model = genAI.getGenerativeModel({ model: "gemini-1.5-pro", generationConfig: { responseMimeType: "application/json" } });
      const response = await model.generateContent([
        promptText,
        { inlineData: { data: imageBase64, mimeType: "image/jpeg" } }
      ]);
      result = response.response.text();
    } else if (aiConfig.provider === 'anthropic') {
      const anthropic = new Anthropic({ apiKey: aiConfig.apiKey });
      const response = await anthropic.messages.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{
          role: "user",
          content: [
            { type: "image", source: { type: "base64", media_type: "image/jpeg", data: imageBase64 } },
            { type: "text", text: promptText + " Respond with JSON only. No markdown formatting like ```json." }
          ]
        }]
      });
      result = response.content[0].text;
    }

    console.log(`[AI RESULT] ${result}`);
    if (result && agentConnected && agentSocketId) {
      let cleanResult = result.trim();
      if (cleanResult.startsWith("```json")) cleanResult = cleanResult.substring(7);
      if (cleanResult.startsWith("```")) cleanResult = cleanResult.substring(3);
      if (cleanResult.endsWith("```")) cleanResult = cleanResult.slice(0, -3);
      
      const parsed = JSON.parse(cleanResult);
      if (parsed.action && parsed.action !== "nothing") {
        io.to(agentSocketId).emit('execute_json_command', parsed);
        io.emit('error_msg', `AI action dispatched: ${parsed.action}`);
      }
    }
  } catch (err) {
    console.error("[AI ERROR]", err);
    io.emit('error_msg', `AI Error: ${err.message}`);
  } finally {
    isAIBusy = false;
  }
}
