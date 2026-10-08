# 🚀 Deployment Guide - MyCiTi Bus Timetable

## Quick Start - 5 Minutes to Live! ⚡

Your app is **ready to deploy** with the beautiful new UI! Choose Render for the easiest experience.

---

## Option 1: Render (Recommended) ⭐

**Why Render:**
- ✅ 750 hours/month FREE (enough for 24/7)
- ✅ Auto HTTPS certificate
- ✅ Auto deploys from GitHub
- ✅ Zero configuration needed
- ✅ `render.yaml` already configured!

### Deploy in 5 Steps:

**Step 1: Push to GitHub**
```bash
cd /Users/dumisani/Documents/Code\ \&\ Projects/github/mycity-bus-schedule
git add .
git commit -m "Ready for deployment with modern UI"
git push origin main
```

**Step 2: Sign Up at Render**
- Go to https://render.com
- Click "Get Started for Free"
- Sign in with GitHub (easiest)

**Step 3: Create Web Service**
- Click "New +" → "Web Service"
- Click "Connect account" to connect GitHub
- Find and select your repo: `mycity-bus-schedule`
- Render auto-detects your `render.yaml` ✅

**Step 4: Configure (auto-filled!)**
- **Name:** myciti-bus-timetable
- **Branch:** main
- **Plan:** Free
- Everything else is pre-configured in `render.yaml`!

**Step 5: Optional - Add Load Shedding API**
- Go to "Environment" tab
- Add variable: `ESP_API_KEY` = `your_eskomsepush_key`
- (Get key at https://eskomsepush.gumroad.com/l/api)
- Skip this if you don't want load shedding features

**Step 6: Deploy! 🚀**
- Click "Create Web Service"
- Wait 2-3 minutes for build
- **Your app is live!** 🎉

Your URL: `https://myciti-bus-timetable.onrender.com`

---

## Option 2: Railway (Also Great!)

**Features:**
- ✅ $5 free credit/month
- ✅ Very fast deploys
- ✅ Simple interface

### Steps:

1. **Sign up:** https://railway.app → Login with GitHub
2. **New Project:** Click "New Project" → "Deploy from GitHub repo"
3. **Select repo:** Choose `mycity-bus-schedule`
4. **Add environment:**
   - Key: `PORT` Value: `8000`
   - Key: `ESP_API_KEY` Value: `your_key` (optional)
5. **Settings → Generate Domain**

Your URL: `myciti-bus-timetable.up.railway.app`

---

## Option 3: Fly.io (Global Edge)

**Features:**
- ✅ 3 free VMs
- ✅ Global deployment

### Steps:

```bash
# Install CLI
brew install flyctl

# Login
fly auth login

# Deploy
fly launch
# Say YES to defaults
# Choose region (closest to Cape Town: Johannesburg)

# Deploy
fly deploy
```

Your URL: `myciti-bus-timetable.fly.dev`

---

## 🔍 Pre-Deployment Checklist

Before deploying, verify:

```bash
# Test locally
uvicorn fastapi_app.main:app --port 8000 --reload

# Open browser
open http://localhost:8000

# Check journey search works
# Check modern UI loads correctly
# Check CSS/images load
```

If everything works locally, it'll work on Render! ✅

---

## 📊 After Deployment

### Test Your Live App:

1. **Homepage:** `https://your-app.onrender.com/`
2. **Search:** `https://your-app.onrender.com/?from_stop=Civic+Centre&to_stop=Camps+Bay&day_type=weekday`
3. **System Map:** `https://your-app.onrender.com/map`

### Monitor in Render Dashboard:
- **Logs:** Real-time application logs
- **Metrics:** CPU, memory usage
- **Deploys:** History of all deployments

---

## 🎨 Custom Domain (Optional)

Want `myciti.yourdomain.com`?

1. Buy domain at Namecheap/Google Domains (~$10/year)
2. In Render: Settings → Custom Domain → Add
3. Update your domain's DNS:
   - Type: `CNAME`
   - Name: `myciti` (or `@` for root)
   - Value: `myciti-bus-timetable.onrender.com`
4. Wait 5-60 minutes for DNS propagation
5. SSL auto-configured! 🔒

---

## 🐛 Troubleshooting

### App won't start?
**Check Render logs:**
- Dashboard → Your service → Logs tab
- Look for errors in red

**Common issues:**
```bash
# Missing dependencies?
# → Add to requirements.txt and redeploy

# Database file missing?
# → Ensure data/myciti.duckdb is in repo

# Port error?
# → render.yaml uses $PORT (correct) ✅
```

### Slow first load?
- Free tier "spins down" after 15 min inactivity
- First request takes ~30 seconds (cold start)
- Subsequent requests are instant!

### Static files not loading?
- Check `fastapi_app/main.py` has:
  ```python
  app.mount("/static", StaticFiles(directory="fastapi_app/static"), name="static")
  ```
- Verify files exist in `fastapi_app/static/`

---

## 💡 Pro Tips

**Keep your free tier:**
- Render gives 750 hrs/month free
- That's enough for ONE app running 24/7
- Use one web service

**Faster deploys:**
- Render caches pip installs
- Second deploy takes ~30 seconds

**Auto-deploy from GitHub:**
- Every push to `main` auto-deploys
- Perfect for continuous updates
- Disable in Settings if needed

**Database:**
- DuckDB file is in your repo (committed)
- No separate database needed
- Updates: run `python run_etl.py` locally, commit new DB

---

## 🚀 Recommended: Deploy to Render Now!

**Why wait?** Deploy in 5 minutes:

1. ✅ Your `render.yaml` is ready
2. ✅ Your beautiful UI is ready  
3. ✅ Your database is ready
4. ✅ Everything is configured!

**Just:**
```bash
git push origin main
```

**Then:** Go to render.com → New Web Service → Select repo → Deploy!

---

## 📈 Scaling (Future)

When your app grows:

**Render Paid Plans:**
- Starter: $7/month (never sleeps, more RAM)
- Standard: $25/month (autoscaling, analytics)

**Alternatives:**
- AWS Elastic Beanstalk
- Google Cloud Run
- Azure App Service
- DigitalOcean App Platform

But start with FREE! 🎉

---

**Questions?** Check:
- Render Docs: https://render.com/docs/deploy-fastapi
- FastAPI Deployment: https://fastapi.tiangolo.com/deployment/

**Ready to go live? Let's deploy! 🚀**
