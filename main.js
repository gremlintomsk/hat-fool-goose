import * as THREE from 'three';

// ---------- ассеты ----------

const CHARACTERS = [
  { id: 'hat',   name: 'ШЛЯПА', src: 'img/1.png' },
  { id: 'goose', name: 'ГУСЬ',  src: 'img/2.png' },
  { id: 'loh',   name: 'ЛОХ',   src: 'img/3.png' },
];
const CARD_BACK_SRC = 'img/4.png';
const TABLE_SRC = 'img/5.png';

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('failed to load ' + src));
    img.src = src;
  });
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// картинка карты → текстура со скруглёнными прозрачными углами
function makeCardTexture(img) {
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const ctx = c.getContext('2d');
  const inset = img.width * .012;
  roundRect(ctx, inset, inset, c.width - inset * 2, c.height - inset * 2, img.width * .07);
  ctx.clip();
  ctx.drawImage(img, 0, 0);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

function makeGlowTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 256;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(128, 128, 20, 128, 128, 128);
  g.addColorStop(0, 'rgba(255,215,120,.9)');
  g.addColorStop(.6, 'rgba(255,190,80,.35)');
  g.addColorStop(1, 'rgba(255,190,80,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(c);
}

const [hatImg, gooseImg, lohImg, backImg, tableImg] = await Promise.all(
  [CHARACTERS[0].src, CHARACTERS[1].src, CHARACTERS[2].src, CARD_BACK_SRC, TABLE_SRC].map(loadImage)
);
const frontTextures = new Map([
  ['hat', makeCardTexture(hatImg)],
  ['goose', makeCardTexture(gooseImg)],
  ['loh', makeCardTexture(lohImg)],
]);
const backTexture = makeCardTexture(backImg);
const glowTexture = makeGlowTexture();

// ---------- сцена ----------

const canvas = document.getElementById('game');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#120d08');
scene.fog = new THREE.Fog('#120d08', 20, 45);

const camera = new THREE.PerspectiveCamera(46, window.innerWidth / window.innerHeight, .1, 100);
camera.position.set(0, 4.2, 10.5);
camera.lookAt(0, 1.6, 0);

// на узких экранах отъезжаем, чтобы все три карты влезали по ширине
function fitCamera() {
  camera.aspect = window.innerWidth / window.innerHeight;
  const halfWidth = 5.2; // край боковой карты + запас
  const needed = halfWidth / (Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.aspect);
  camera.position.z = Math.max(10.5, needed);
  camera.updateProjectionMatrix();
}
fitCamera();

// игральный стол
{
  const tableTexture = new THREE.Texture(tableImg);
  tableTexture.needsUpdate = true;
  tableTexture.colorSpace = THREE.SRGBColorSpace;
  tableTexture.anisotropy = 8;
  const table = new THREE.Mesh(
    new THREE.PlaneGeometry(27, 18),
    new THREE.MeshBasicMaterial({ map: tableTexture })
  );
  table.rotation.x = -Math.PI / 2;
  table.position.z = -1.5;
  scene.add(table);
}

// ---------- карты ----------

const CARD_W = 2.7, CARD_H = 3.6;
const BASE_Y = 2.35;

const cards = [];
const pickables = [];

for (let i = 0; i < 3; i++) {
  const x = (i - 1) * 3.5;
  const group = new THREE.Group();
  group.position.set(x, BASE_Y, 0);
  group.rotation.z = (i - 1) * -.06;

  const geo = new THREE.PlaneGeometry(CARD_W, CARD_H);
  const front = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ transparent: true }));
  front.position.z = .012;
  const back = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: backTexture, transparent: true }));
  back.position.z = -.012;
  back.rotation.y = Math.PI;
  group.add(front, back);
  group.rotation.y = Math.PI; // рубашкой к игроку

  const glow = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_W * 1.7, CARD_H * 1.5),
    new THREE.MeshBasicMaterial({ map: glowTexture, transparent: true, opacity: 0, depthWrite: false })
  );
  glow.position.set(x, BASE_Y, -.2);
  scene.add(glow, group);

  front.userData.cardIndex = i;
  back.userData.cardIndex = i;
  pickables.push(front, back);
  cards.push({ group, front, glow, baseX: x, character: null, flipped: false });
}

// ---------- твины ----------

const tweens = [];
function tween(obj, prop, to, dur, ease = easeOutCubic, done) {
  tweens.push({ obj, prop, from: obj[prop], to, dur, t: 0, ease, done });
}
function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }
function easeOutBack(t) { const c = 1.7; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); }

function updateTweens(dt) {
  for (let i = tweens.length - 1; i >= 0; i--) {
    const tw = tweens[i];
    tw.t += dt / tw.dur;
    const k = tw.t >= 1 ? 1 : tw.ease(tw.t);
    tw.obj[tw.prop] = tw.from + (tw.to - tw.from) * k;
    if (tw.t >= 1) {
      tweens.splice(i, 1);
      if (tw.done) tw.done();
    }
  }
}

// ---------- фейерверки ----------

const fireworks = [];
const FW_COLORS = [0xffd166, 0xef476f, 0x06d6a0, 0x4cc9f0, 0xffa8f0, 0xffffff];

function spawnBurst() {
  const n = 140;
  const pos = new Float32Array(n * 3);
  const vel = [];
  const origin = new THREE.Vector3((Math.random() - .5) * 10, 3.5 + Math.random() * 3, .5 - Math.random() * 2.5);
  for (let i = 0; i < n; i++) {
    pos[i * 3] = origin.x; pos[i * 3 + 1] = origin.y; pos[i * 3 + 2] = origin.z;
    const dir = new THREE.Vector3(Math.random() - .5, Math.random() - .5, Math.random() - .5).normalize();
    vel.push(dir.multiplyScalar(2.5 + Math.random() * 4.5));
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.PointsMaterial({
    color: FW_COLORS[Math.floor(Math.random() * FW_COLORS.length)],
    size: .28, transparent: true, opacity: 1,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const points = new THREE.Points(geo, mat);
  scene.add(points);
  fireworks.push({ points, vel, life: 0 });
}

function updateFireworks(dt) {
  for (let i = fireworks.length - 1; i >= 0; i--) {
    const fw = fireworks[i];
    fw.life += dt;
    const p = fw.points.geometry.attributes.position;
    for (let j = 0; j < fw.vel.length; j++) {
      const v = fw.vel[j];
      v.y -= 3.5 * dt;
      v.multiplyScalar(1 - .8 * dt);
      p.array[j * 3] += v.x * dt;
      p.array[j * 3 + 1] += v.y * dt;
      p.array[j * 3 + 2] += v.z * dt;
    }
    p.needsUpdate = true;
    fw.points.material.opacity = Math.max(0, 1 - fw.life / 1.8);
    if (fw.life > 1.8) {
      scene.remove(fw.points);
      fw.points.geometry.dispose();
      fw.points.material.dispose();
      fireworks.splice(i, 1);
    }
  }
}

let fireworksTimer = null;
function startFireworks() {
  spawnBurst(); spawnBurst();
  let shots = 0;
  fireworksTimer = setInterval(() => {
    spawnBurst();
    if (++shots > 30) { clearInterval(fireworksTimer); fireworksTimer = null; }
  }, 380);
}
function stopFireworks() {
  if (fireworksTimer) { clearInterval(fireworksTimer); fireworksTimer = null; }
}

// ---------- игра ----------

const questionTarget = document.getElementById('target');
const overlay = document.getElementById('overlay');
const winText = document.getElementById('winText');
const againBtn = document.getElementById('again');
const attemptsEl = document.getElementById('attempts');
const scoreEl = document.getElementById('score');
const streakEl = document.getElementById('streak');
const gainTextEl = document.getElementById('gainText');
const finalScreen = document.getElementById('finalScreen');
const finalScoreEl = document.getElementById('finalScore');
const finalNoteEl = document.getElementById('finalNote');

const MAX_ATTEMPTS = 10;
const BASE_POINTS = 100;

let target = null;
let locked = false;
let hovered = -1;
let started = false;
let attempt = 0;
let score = 0;
let streak = 0;

function updateHud() {
  attemptsEl.textContent = `Попытка ${Math.min(attempt + 1, MAX_ATTEMPTS)}/${MAX_ATTEMPTS}`;
  scoreEl.textContent = `Очки: ${score}`;
  streakEl.textContent = `серия ×${streak}`;
  streakEl.classList.toggle('shown', streak >= 2);
}

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function newRound() {
  stopFireworks();
  overlay.className = '';
  winText.classList.remove('shown');
  gainTextEl.classList.remove('shown');
  updateHud();
  locked = true;
  const order = shuffle([...CHARACTERS]);
  target = CHARACTERS[Math.floor(Math.random() * CHARACTERS.length)];
  questionTarget.textContent = target.name;
  cards.forEach((card, i) => {
    card.character = order[i];
    card.front.material.map = frontTextures.get(order[i].id);
    card.front.material.needsUpdate = true;
    if (card.flipped) tween(card.group.rotation, 'y', Math.PI, .5);
    card.flipped = false;
    tween(card.group.position, 'y', BASE_Y, .5);
    tween(card.group.scale, 'x', 1, .3);
    tween(card.group.scale, 'y', 1, .3);
  });
  setTimeout(() => { locked = false; }, 600);
}

function flipCard(card, delay, done) {
  setTimeout(() => {
    card.flipped = true;
    tween(card.group.position, 'y', BASE_Y + .55, .28, easeOutCubic, () => {
      tween(card.group.position, 'y', BASE_Y, .35);
    });
    tween(card.group.rotation, 'y', 0, .55, easeOutBack, done);
  }, delay);
}

function pick(index) {
  if (!started || locked) return;
  locked = true;
  setHover(-1);
  attempt++;
  const card = cards[index];
  const win = card.character.id === target.id;
  flipCard(card, 0, () => {
    // показать остальные
    cards.forEach((c, i) => { if (i !== index) flipCard(c, 250 + Math.abs(i - index) * 120); });
    setTimeout(() => finish(win), 900);
  });
}

function finish(win) {
  if (win) {
    streak++;
    const gain = BASE_POINTS * streak;
    score += gain;
    gainTextEl.textContent = streak >= 2 ? `+${gain} очков (серия ×${streak})` : `+${gain} очков`;
    gainTextEl.classList.add('shown');
    winText.classList.add('shown');
    overlay.className = 'win shown';
    startFireworks();
  } else {
    streak = 0;
    overlay.className = 'lose shown';
  }
  updateHud();
  againBtn.textContent = attempt >= MAX_ATTEMPTS ? 'Итоги' : 'Дальше';
}

function showFinal() {
  stopFireworks();
  finalScoreEl.textContent = `${score} очков`;
  const maxScore = BASE_POINTS * (MAX_ATTEMPTS * (MAX_ATTEMPTS + 1)) / 2;
  finalNoteEl.textContent = score >= maxScore / 2 ? 'Ты не лох, ты молодец!' : score > 0 ? 'Могло быть и хуже' : 'Ты АБСОЛЮТНЫЙ ЛЛЛЛОХХ!';
  finalScreen.classList.add('shown');
  if (score > 0) submitScore(score);
}

againBtn.addEventListener('click', () => {
  if (attempt >= MAX_ATTEMPTS) showFinal();
  else newRound();
});

document.getElementById('restart').addEventListener('click', () => {
  attempt = 0; score = 0; streak = 0;
  finalScreen.classList.remove('shown');
  newRound();
});

// ---------- рейтинг и вход через Google ----------

// центры строк пергамента на img/8.png, в % высоты картинки
const LB_ROW_CENTERS = [29.3, 36.3, 43.4, 50.3, 57.6, 64.7, 71.8];

const lbScreen = document.getElementById('lbScreen');
const lbWrap = document.getElementById('lbWrap');
const lbRowsEl = document.getElementById('lbRows');
const lbAuth = document.getElementById('lbAuth');
const lbHint = document.getElementById('lbHint');

let googleToken = null;
let pendingScore = 0;

function initGoogle() {
  const id = window.GAME_CONFIG && window.GAME_CONFIG.googleClientId;
  const btns = [document.getElementById('gsiBtnIntro'), document.getElementById('gsiBtn')];
  if (!id) {
    lbHint.textContent = 'Вход через Google не настроен';
    btns.forEach(b => b.style.display = 'none');
    return;
  }
  let tries = 0;
  const timer = setInterval(() => {
    if (window.google && google.accounts && google.accounts.oauth2) {
      clearInterval(timer);
      const client = google.accounts.oauth2.initTokenClient({
        client_id: id,
        scope: 'openid profile',
        callback: (resp) => {
          if (!resp.access_token) return;
          googleToken = resp.access_token;
          lbAuth.classList.add('signed');
          lbHint.textContent = 'Ты в игре — очки идут в рейтинг';
          btns.forEach(b => b.style.display = 'none');
          if (pendingScore > 0) submitScore(pendingScore).then(refreshLeaderboard);
        },
      });
      btns.forEach(b => b.addEventListener('click', (e) => {
        e.stopPropagation();
        client.requestAccessToken();
      }));
      lbHint.textContent = 'Войди, чтобы попасть в рейтинг';
    } else if (++tries > 50) {
      clearInterval(timer);
      lbHint.textContent = 'Google недоступен';
      btns.forEach(b => b.style.display = 'none');
    }
  }, 200);
}

async function submitScore(s) {
  if (!googleToken) { pendingScore = Math.max(pendingScore, s); return; }
  try {
    await fetch('/api/score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: googleToken, score: s }),
    });
    pendingScore = 0;
  } catch (e) { /* рейтинг недоступен — не мешаем игре */ }
}

function renderLeaderboard(list) {
  lbRowsEl.innerHTML = '';
  const fs = lbWrap.querySelector('img').clientHeight * .04;
  LB_ROW_CENTERS.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'lbRow';
    row.style.top = `${c - 3.3}%`;
    row.style.fontSize = `${fs}px`;
    const p = list[i];
    row.innerHTML = p
      ? `<span class="n"></span><span class="s">${p.score}</span>`
      : '<span class="n"></span><span class="s"></span>';
    if (p) row.querySelector('.n').textContent = p.name;
    lbRowsEl.appendChild(row);
  });
}

async function refreshLeaderboard() {
  let list = [];
  try { list = await (await fetch('/api/leaderboard')).json(); } catch (e) { /* оффлайн */ }
  renderLeaderboard(list);
}

function openLeaderboard(e) {
  e.stopPropagation();
  lbScreen.classList.add('shown');
  refreshLeaderboard();
}

document.getElementById('lbBtn').addEventListener('click', openLeaderboard);
document.getElementById('lbBtn2').addEventListener('click', openLeaderboard);
document.getElementById('lbClose').addEventListener('click', (e) => {
  e.stopPropagation();
  lbScreen.classList.remove('shown');
});
window.addEventListener('resize', () => {
  if (lbScreen.classList.contains('shown')) refreshLeaderboard();
});

initGoogle();

const startScreen = document.getElementById('startScreen');
document.getElementById('startBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  started = true;
  startScreen.classList.add('hidden');
});

// ---------- ввод ----------

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2(-10, -10);
let mouseX = 0;

function updatePointer(e) {
  pointer.x = (e.clientX / window.innerWidth) * 2 - 1;
  pointer.y = -(e.clientY / window.innerHeight) * 2 + 1;
  mouseX = pointer.x;
}

function setHover(index) {
  if (hovered === index) return;
  if (hovered >= 0) {
    const c = cards[hovered];
    tween(c.group.position, 'y', BASE_Y, .25);
    tween(c.group.scale, 'x', 1, .25);
    tween(c.group.scale, 'y', 1, .25);
    tween(c.glow.material, 'opacity', 0, .25);
  }
  hovered = index;
  if (hovered >= 0) {
    const c = cards[hovered];
    tween(c.group.position, 'y', BASE_Y + .3, .25);
    tween(c.group.scale, 'x', 1.07, .25);
    tween(c.group.scale, 'y', 1.07, .25);
    tween(c.glow.material, 'opacity', .95, .25);
  }
  document.body.style.cursor = hovered >= 0 ? 'pointer' : 'default';
}

window.addEventListener('pointermove', updatePointer);
window.addEventListener('click', (e) => {
  updatePointer(e);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(pickables)[0];
  if (hit) pick(hit.object.userData.cardIndex);
});
window.addEventListener('resize', () => {
  fitCamera();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ---------- цикл ----------

const clock = new THREE.Clock();

function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), .05);
  updateTweens(dt);
  updateFireworks(dt);

  if (started && !locked) {
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(pickables)[0];
    setHover(hit ? hit.object.userData.cardIndex : -1);
  }

  // лёгкий параллакс камеры
  camera.position.x += (mouseX * .6 - camera.position.x) * .04;
  camera.lookAt(0, 1.6, 0);

  // покачивание карт
  const t = clock.elapsedTime;
  cards.forEach((c, i) => {
    c.group.rotation.x = Math.sin(t * .9 + i * 2.1) * .02;
  });

  renderer.render(scene, camera);
}

newRound();
animate();
