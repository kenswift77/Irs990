-- ============================================================================
-- IRS 990 MCP Server — Client / User / Request-Log schema
-- Run once against your Azure SQL database.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- mcp_clients
--   One row per organisation / application that has been granted access.
--   The API key is stored only as a SHA-256 hex hash — the plain-text key
--   is shown to the administrator once at creation time and never stored.
-- ----------------------------------------------------------------------------
CREATE TABLE dbo.mcp_clients
(
    id              INT            IDENTITY(1,1) NOT NULL,
    client_name     NVARCHAR(200)  NOT NULL,
    api_key_hash    CHAR(64)       NOT NULL,   -- SHA-256 hex of the raw key
    api_key_prefix  VARCHAR(12)    NOT NULL,   -- first 8 chars (for UI display)
    is_active       BIT            NOT NULL CONSTRAINT DF_mcp_clients_is_active   DEFAULT 1,
    created_at      DATETIME2(0)   NOT NULL CONSTRAINT DF_mcp_clients_created_at  DEFAULT SYSUTCDATETIME(),
    notes           NVARCHAR(500)  NULL,

    CONSTRAINT PK_mcp_clients PRIMARY KEY CLUSTERED (id),
    CONSTRAINT UQ_mcp_clients_api_key_hash UNIQUE (api_key_hash)
);

-- ----------------------------------------------------------------------------
-- mcp_client_users
--   Known individuals within a client organisation.
--   A user is matched by the value the client sends in X-User-Id.
--   Rows here are optional — if the header is present but the user isn't
--   registered, the raw identifier is still captured in the request log.
-- ----------------------------------------------------------------------------
CREATE TABLE dbo.mcp_client_users
(
    id               INT            IDENTITY(1,1) NOT NULL,
    client_id        INT            NOT NULL,
    user_identifier  NVARCHAR(200)  NOT NULL,  -- email or username sent by the client
    display_name     NVARCHAR(200)  NULL,
    is_active        BIT            NOT NULL CONSTRAINT DF_mcp_client_users_is_active  DEFAULT 1,
    created_at       DATETIME2(0)   NOT NULL CONSTRAINT DF_mcp_client_users_created_at DEFAULT SYSUTCDATETIME(),

    CONSTRAINT PK_mcp_client_users  PRIMARY KEY CLUSTERED (id),
    CONSTRAINT FK_mcp_client_users_client
        FOREIGN KEY (client_id) REFERENCES dbo.mcp_clients(id),
    CONSTRAINT UQ_mcp_client_users
        UNIQUE (client_id, user_identifier)
);

-- ----------------------------------------------------------------------------
-- mcp_request_log
--   One row per inbound MCP tool call (POST /mcp with method = tools/call).
-- ----------------------------------------------------------------------------
CREATE TABLE dbo.mcp_request_log
(
    id               BIGINT         IDENTITY(1,1) NOT NULL,
    client_id        INT            NOT NULL,
    client_user_id   INT            NULL,           -- FK if user is registered
    user_identifier  NVARCHAR(200)  NULL,           -- raw X-User-Id header value
    tool_name        NVARCHAR(100)  NULL,
    requested_at     DATETIME2(0)   NOT NULL CONSTRAINT DF_mcp_request_log_at DEFAULT SYSUTCDATETIME(),
    duration_ms      INT            NULL,
    status           VARCHAR(20)    NOT NULL CONSTRAINT DF_mcp_request_log_status DEFAULT 'success',
    ip_address       VARCHAR(45)    NULL,
    error_message    NVARCHAR(1000) NULL,

    CONSTRAINT PK_mcp_request_log PRIMARY KEY CLUSTERED (id),
    CONSTRAINT FK_mcp_request_log_client
        FOREIGN KEY (client_id) REFERENCES dbo.mcp_clients(id),
    CONSTRAINT FK_mcp_request_log_client_user
        FOREIGN KEY (client_user_id) REFERENCES dbo.mcp_client_users(id)
);

CREATE NONCLUSTERED INDEX IX_mcp_request_log_client_id
    ON dbo.mcp_request_log (client_id)
    INCLUDE (requested_at, tool_name, status);

CREATE NONCLUSTERED INDEX IX_mcp_request_log_requested_at
    ON dbo.mcp_request_log (requested_at DESC)
    INCLUDE (client_id, tool_name, status);

CREATE NONCLUSTERED INDEX IX_mcp_request_log_tool_name
    ON dbo.mcp_request_log (tool_name)
    INCLUDE (client_id, requested_at);

-- ----------------------------------------------------------------------------
-- Handy summary view
-- ----------------------------------------------------------------------------
GO
CREATE OR ALTER VIEW dbo.vw_mcp_usage_summary AS
SELECT
    c.client_name,
    c.api_key_prefix,
    c.is_active                              AS client_active,
    r.tool_name,
    CAST(r.requested_at AS DATE)             AS request_date,
    COUNT(*)                                 AS request_count,
    AVG(r.duration_ms)                       AS avg_duration_ms,
    SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS error_count
FROM dbo.mcp_request_log r
JOIN dbo.mcp_clients c ON r.client_id = c.id
GROUP BY c.client_name, c.api_key_prefix, c.is_active, r.tool_name,
         CAST(r.requested_at AS DATE);
GO
