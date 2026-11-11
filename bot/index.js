// Word Counter's Discord bot.
//
// Plain English: this logs in as YOUR bot, waits until you (the person who made the bot)
// join a voice channel, joins the same channel (muted - it never talks), and passes each
// person's voice separately to Word Counter. When you leave, it leaves. Discord's voice
// encryption (DAVE) is handled by @discordjs/voice.
//
// Talking to Word Counter happens over stdout in small binary messages:
//   [1 byte kind][4 bytes length, little endian][payload]
//   kind 1 = JSON event      (ready / joined / left / start / end / error / check)
//   kind 2 = voice audio     payload = [1 byte id length][user id][16 kHz mono 16-bit samples]
// Logs go to stderr. Word Counter sends nothing back - it just stops this process to leave.
//
// Usage:  node index.js              (token in the WC_TOKEN environment variable)
//         node index.js --check      (log in, report the bot's name and servers, exit)
//         node index.js --selftest   (offline check that every part loads and audio decoding works)

"use strict";

const log = (...a) => process.stderr.write(a.join(" ") + "\n");

function frame(kind, payload) {
  const head = Buffer.alloc(5);
  head.writeUInt8(kind, 0);
  head.writeUInt32LE(payload.length, 1);
  process.stdout.write(Buffer.concat([head, payload]));
}
const send = (obj) => frame(1, Buffer.from(JSON.stringify(obj), "utf8"));

// 48 kHz stereo 16-bit (what Discord gives) -> 16 kHz mono 16-bit (what the speech engine wants)
function to16kMono(pcm, carry) {
  const buf = carry.length ? Buffer.concat([carry, pcm]) : pcm;
  const frames = Math.floor(buf.length / 4);          // stereo frames
  const groups = Math.floor(frames / 3);              // 3 frames at 48 kHz = 1 sample at 16 kHz
  const out = Buffer.alloc(groups * 2);
  for (let g = 0; g < groups; g++) {
    let sum = 0;
    for (let k = 0; k < 3; k++) {
      const o = (g * 3 + k) * 4;
      sum += buf.readInt16LE(o) + buf.readInt16LE(o + 2);
    }
    out.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(sum / 6))), g * 2);
  }
  return { out, carry: buf.subarray(groups * 12) };
}

async function selftest() {
  const report = {};
  const voice = require("@discordjs/voice");
  report.dependencies = voice.generateDependencyReport();
  require("discord.js");
  report.dave = !!voice.DAVESession;
  const prism = require("prism-media");
  const OpusScript = require("opusscript");
  // encode one 20 ms tone, decode it the same way voice is decoded, and convert to 16 kHz mono
  const enc = new OpusScript(48000, 2, OpusScript.Application.VOIP);
  const tone = Buffer.alloc(960 * 4);
  for (let i = 0; i < 960; i++) {
    const v = Math.round(8000 * Math.sin((2 * Math.PI * 440 * i) / 48000));
    tone.writeInt16LE(v, i * 4);
    tone.writeInt16LE(v, i * 4 + 2);
  }
  const packet = enc.encode(tone, 960);
  const dec = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
  const pcm = await new Promise((resolve, reject) => {
    dec.once("data", resolve);
    dec.once("error", reject);
    dec.write(packet);
  });
  const { out } = to16kMono(pcm, Buffer.alloc(0));
  report.decoded_samples = out.length / 2;
  report.ok = report.dave && report.decoded_samples === 320;
  send({ t: "selftest", ...report });
}

async function main() {
  if (process.argv.includes("--selftest")) return selftest();

  const token = process.env.WC_TOKEN;
  if (!token) { send({ t: "error", msg: "no bot token set" }); process.exit(2); }
  const { Client, GatewayIntentBits, Events } = require("discord.js");
  const { joinVoiceChannel, EndBehaviorType, VoiceConnectionStatus, entersState } = require("@discordjs/voice");
  const prism = require("prism-media");

  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
  let ownerId = null;
  let conn = null;
  let current = null;              // channel id we're in
  const listening = new Map();     // user id -> true while their voice stream is open

  async function nameOf(guild, userId) {
    try {
      const m = guild.members.cache.get(userId) || (await guild.members.fetch(userId));
      return m.displayName;
    } catch {
      try { return (await client.users.fetch(userId)).username; } catch { return "someone"; }
    }
  }

  function leave() {
    if (conn) { try { conn.destroy(); } catch {} }
    conn = null;
    current = null;
    listening.clear();
    send({ t: "left" });
  }

  async function join(channel) {
    if (current === channel.id) return;
    leave();
    current = channel.id;
    log("joining", channel.name);
    conn = joinVoiceChannel({
      channelId: channel.id, guildId: channel.guild.id,
      adapterCreator: channel.guild.voiceAdapterCreator,
      selfDeaf: false, selfMute: true,             // must hear, never talks
    });
    try {
      await entersState(conn, VoiceConnectionStatus.Ready, 20000);
    } catch (e) {
      send({ t: "error", msg: "couldn't join the voice channel - does the bot have permission to Connect?" });
      leave();
      return;
    }
    send({ t: "joined", channel: channel.name, guild: channel.guild.name });
    conn.on(VoiceConnectionStatus.Disconnected, async () => {
      try {   // a short reconnect is normal; give it a moment before giving up
        await Promise.race([entersState(conn, VoiceConnectionStatus.Signalling, 5000),
                            entersState(conn, VoiceConnectionStatus.Connecting, 5000)]);
      } catch { leave(); }
    });
    conn.receiver.speaking.on("start", async (userId) => {
      if (listening.has(userId) || userId === client.user.id) return;
      listening.set(userId, true);
      const name = await nameOf(channel.guild, userId);
      send({ t: "start", id: userId, name });
      const opus = conn.receiver.subscribe(userId, { end: { behavior: EndBehaviorType.AfterSilence, duration: 500 } });
      const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
      let carry = Buffer.alloc(0);
      const idBuf = Buffer.from(userId, "ascii");
      decoder.on("data", (pcm) => {
        const r = to16kMono(pcm, carry);
        carry = r.carry;
        if (r.out.length) frame(2, Buffer.concat([Buffer.from([idBuf.length]), idBuf, r.out]));
      });
      decoder.on("error", (e) => log("decode error", e.message));   // one bad packet shouldn't stop anything
      const finish = () => {
        if (!listening.has(userId)) return;
        listening.delete(userId);
        send({ t: "end", id: userId });
      };
      opus.on("end", finish);
      opus.on("close", finish);
      opus.on("error", (e) => { log("receive error", e.message); finish(); });
      opus.pipe(decoder);
    });
  }

  function findOwner() {
    for (const guild of client.guilds.cache.values()) {
      const vs = guild.voiceStates.cache.get(ownerId);
      if (vs && vs.channel) return vs.channel;
    }
    return null;
  }

  client.once(Events.ClientReady, async (c) => {
    const app = await c.application.fetch();
    const owner = app.owner;
    ownerId = owner ? (owner.ownerId || owner.id) : null;       // a team owner has ownerId
    const guilds = [...c.guilds.cache.values()].map((g) => g.name);
    if (process.argv.includes("--check")) {
      send({ t: "check", bot: c.user.tag, id: c.user.id, owner: owner ? (owner.username || owner.name) : null,
             guilds });
      await c.destroy();
      process.exit(0);
    }
    send({ t: "ready", bot: c.user.tag, guilds });
    const ch = findOwner();
    if (ch) join(ch);
  });

  client.on(Events.VoiceStateUpdate, (before, after) => {
    if (after.id !== ownerId) return;
    if (after.channel && after.channelId !== current) join(after.channel);
    else if (!after.channel && current) leave();
  });

  client.on(Events.Error, (e) => log("client error", e.message));
  try {
    await client.login(token);
  } catch (e) {
    send({ t: "error", msg: /token/i.test(e.message) ? "that bot token doesn't work - copy it again" : e.message });
    process.exit(3);
  }
  const quit = () => { leave(); client.destroy().finally(() => process.exit(0)); };
  process.on("SIGTERM", quit);
  process.on("SIGINT", quit);
  process.stdin.on("end", quit);       // Word Counter closed - leave the call
  process.stdin.resume();
}

main().catch((e) => { send({ t: "error", msg: e.message }); process.exit(1); });
