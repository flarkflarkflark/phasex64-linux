-- SPDX-License-Identifier: GPL-2.0-only
-- Copyright (C) 2026 flarkflarkflark
--[[
Automated REAPER test for snd-usb-phasex64 (v0.1.8).

Track 1 plays a pre-generated 1kHz tone (reaper_run/tone_1khz.wav) routed
to hardware outputs 3/4 via a hardware output send (master send disabled).
Track 2 records hardware inputs 3/4 (external RCA loopback cable),
armed, record mode = input. Sequence: 10s initial recording, save
project, then 5x 2s record/stop cycles to exercise open/close/prepare.
Every step is logged via reaper.ShowConsoleMsg and to reaper_run/
reaper_test_log.txt, then REAPER quits (Main_OnCommand 40004).

ReaScript API usage below (I_RECINPUT bit encoding, hardware-output-send
category, action command IDs, RECORD_PATH project key, Main_SaveProjectEx/
RecursiveCreateDirectory signatures) was verified against REAPER API
documentation and real working examples before writing this, not guessed.
--]]

local SCRIPT_DIR = debug.getinfo(1, "S").source:match("@(.*[/\\])") or "./"
local RUN_DIR = SCRIPT_DIR .. "reaper_run/"
local LOG_PATH = RUN_DIR .. "reaper_test_log.txt"
local TONE_WAV = RUN_DIR .. "tone_1khz.wav"
local PROJECT_PATH = RUN_DIR .. "reaper_test_project.rpp"

reaper.RecursiveCreateDirectory(RUN_DIR, 0)

local log_file = io.open(LOG_PATH, "w")

local function log(msg)
	local line = string.format("[%.3f] %s", reaper.time_precise(), msg)
	reaper.ShowConsoleMsg(line .. "\n")
	if log_file then
		log_file:write(line .. "\n")
		log_file:flush()
	end
end

log("=== reaper_test.lua starting ===")

-- 1. new project
reaper.Main_OnCommand(40023, 0) -- File: New project
log("new project created")

-- recorded audio lands directly in reaper_run/, absolute path, no ambiguity
local rp_ok, rp_val = reaper.GetSetProjectInfo_String(0, "RECORD_PATH", RUN_DIR, true)
log(string.format("RECORD_PATH set -> ok=%s", tostring(rp_ok)))

-- 2. track 1: playback of 1kHz tone to hw out 3/4
reaper.InsertTrackAtIndex(0, true)
local tr1 = reaper.GetTrack(0, 0)
reaper.GetSetMediaTrackInfo_String(tr1, "P_NAME", "Playback 1kHz -> hw3-4", true)
reaper.SetOnlyTrackSelected(tr1)
reaper.SetEditCurPos(0, false, false)
local ins_ok = reaper.InsertMedia(TONE_WAV, 0) -- 0 = add to current track
log(string.format("InsertMedia(%s) on track1 -> %s", TONE_WAV, tostring(ins_ok)))

-- disable master send, add a hardware-output send to channels 3/4
-- (0-based dest channel 2) instead
reaper.SetMediaTrackInfo_Value(tr1, "B_MAINSEND", 0)
local sendidx = reaper.CreateTrackSend(tr1, nil) -- nil dest = hardware output
reaper.SetTrackSendInfo_Value(tr1, 1, sendidx, "I_DSTCHAN", 2) -- category 1 = hw output
local dstchan_rb = reaper.GetTrackSendInfo_Value(tr1, 1, sendidx, "I_DSTCHAN")
log(string.format("track1 hw send idx=%d I_DSTCHAN readback=%d (hw out 3/4)",
	sendidx, dstchan_rb))

-- 3. track 2: record hw in 3/4 (stereo pair starting at 0-based channel 2)
reaper.InsertTrackAtIndex(1, true)
local tr2 = reaper.GetTrack(0, 1)
reaper.GetSetMediaTrackInfo_String(tr2, "P_NAME", "Record hw3-4", true)
reaper.SetMediaTrackInfo_Value(tr2, "I_RECMODE", 0) -- 0 = input
reaper.SetMediaTrackInfo_Value(tr2, "I_RECINPUT", 1024 + 2) -- 1024=stereo pair flag, 2=0-based first channel
reaper.SetMediaTrackInfo_Value(tr2, "I_RECARM", 1)
local rb_input = reaper.GetMediaTrackInfo_Value(tr2, "I_RECINPUT")
local rb_mode = reaper.GetMediaTrackInfo_Value(tr2, "I_RECMODE")
local rb_arm = reaper.GetMediaTrackInfo_Value(tr2, "I_RECARM")
log(string.format("track2 configured: I_RECINPUT=%d I_RECMODE=%d I_RECARM=%d",
	rb_input, rb_mode, rb_arm))

reaper.UpdateArrange()

-- state machine driven by a deferred loop - a Lua script can't block on a
-- real-time sleep without freezing REAPER's UI/audio thread
local state = "pre_record_wait"
local state_start = reaper.time_precise()
local phase_logged = false
local cycles_done = 0
local CYCLE_COUNT = 5
local CYCLE_DUR = 2.0
local CYCLE_GAP = 0.3

local function elapsed()
	return reaper.time_precise() - state_start
end

local function goto_state(name)
	state = name
	state_start = reaper.time_precise()
	phase_logged = false
end

local function main_loop()
	if state == "pre_record_wait" then
		if not phase_logged then
			log("waiting 1s before starting transport")
			phase_logged = true
		end
		if elapsed() > 1.0 then
			log("starting initial 10s recording (Main_OnCommand 1013)")
			reaper.Main_OnCommand(1013, 0) -- Transport: Record
			goto_state("recording")
		end

	elseif state == "recording" then
		if elapsed() > 10.0 then
			reaper.Main_OnCommand(1016, 0) -- Transport: Stop
			log("stopped initial 10s recording (Main_OnCommand 1016)")
			goto_state("post_record_save")
		end

	elseif state == "post_record_save" then
		if elapsed() > 1.0 then
			local save_ok = reaper.Main_SaveProjectEx(0, PROJECT_PATH, 0)
			log(string.format("project saved to %s -> %s", PROJECT_PATH, tostring(save_ok)))
			goto_state("start_cycle")
		end

	elseif state == "start_cycle" then
		cycles_done = cycles_done + 1
		if cycles_done > CYCLE_COUNT then
			goto_state("finish")
		else
			log(string.format("cycle %d/%d: transport start (record)", cycles_done, CYCLE_COUNT))
			reaper.Main_OnCommand(1013, 0) -- Transport: Record
			goto_state("cycle_running")
		end

	elseif state == "cycle_running" then
		if elapsed() > CYCLE_DUR then
			reaper.Main_OnCommand(1016, 0) -- Transport: Stop
			log(string.format("cycle %d/%d: transport stop", cycles_done, CYCLE_COUNT))
			goto_state("cycle_gap")
		end

	elseif state == "cycle_gap" then
		if elapsed() > CYCLE_GAP then
			goto_state("start_cycle")
		end

	elseif state == "finish" then
		local save_ok = reaper.Main_SaveProjectEx(0, PROJECT_PATH, 0)
		log(string.format("final project save -> %s", tostring(save_ok)))
		log("=== reaper_test.lua complete, quitting REAPER ===")
		if log_file then
			log_file:flush()
			log_file:close()
			log_file = nil
		end
		reaper.Main_OnCommand(40004, 0) -- File: Quit REAPER
		return -- do not reschedule
	end

	reaper.defer(main_loop)
end

main_loop()
