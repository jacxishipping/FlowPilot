-- Enable extensions for UUID generation, cryptography, and pgvector agent memory
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- Create Temporal databases alongside primary FlowPilot database
CREATE DATABASE temporal;
CREATE DATABASE temporal_visibility;
