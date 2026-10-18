# 🏗️ Marcus System Architecture Documentation

Technical deep-dive documentation for all 55 systems powering Marcus's AI agent coordination platform.

---

## 📋 **System Overview**

Marcus is built on 55 interconnected systems that enable AI agents to collaborate autonomously. Each system is documented with implementation details, APIs, and integration patterns.

### **🎯 Quick Navigation**

| System Category | Files | Description |
|-----------------|-------|-------------|
| **Core Intelligence** | 01, 07, 17, 23, 27, 44 | Memory, AI engine, learning, task intelligence, recommendations, task classifier |
| **Agent Coordination** | 21, 26, 03, 12 | Agent management, worker support, context sharing, communication |
| **Project Management** | 16, 04, 24, 25, 53 | Project control, Kanban integration, analysis, reporting, workflow management |
| **Development & Analysis** | 42, 43 | Code analysis, API systems |
| **Data & Storage** | 10, 32, 13, 19 | Persistence, models, cost tracking, NLP processing |
| **Security & Compliance** | 51 | Security systems, access control, threat detection |
| **Quality & Testing** | 18, 30, 11, 29 | QA systems, testing framework, monitoring, detection |
| **Infrastructure** | 08, 09, 06, 14, 15 | Error handling, events, MCP server, isolation, service registry |
| **Operations** | 22, 20, 28, 31 | Operational modes, pipelines, configuration, resilience |
| **Visualization** | 05, 02 | UI systems, logging and visualization |

---

## 🧠 **Core Intelligence Systems**

### **[01 - Memory System](intelligence/01-memory-system.md)**
Multi-tier cognitive memory (Working, Episodic, Semantic, Procedural) that enables learning and context retention across projects.

### **[07 - AI Intelligence Engine](intelligence/07-ai-intelligence-engine.md)**
Hybrid AI decision-making system combining multiple AI providers with context-aware prompt engineering.

### **[17 - Learning Systems](intelligence/17-learning-systems.md)**
Continuous learning from project outcomes, pattern recognition, and performance optimization.

### **[23 - Task Management Intelligence](intelligence/23-task-management-intelligence.md)**
Intelligent task analysis, dependency inference, and automatic task breakdown.

### **[27 - Recommendation Engine](intelligence/27-recommendation-engine.md)**
AI-powered recommendations for task assignment, technology choices, and workflow optimization.

### **[44 - Enhanced Task Classifier](intelligence/44-enhanced-task-classifier.md)**
Intelligent task categorization, priority scoring, and agent matching using ML and NLP.

---

## 🤖 **Agent Coordination Systems**

### **[21 - Agent Coordination](coordination/21-agent-coordination.md)**
Core agent lifecycle management, registration, assignment, and the work-loop protocol. *(What earlier docs called "Communication Hub" is folded in here — there is no separate inter-agent message bus; coordination happens on the board.)*

### **[26 - Worker Support](coordination/26-worker-support.md)**
Tools and utilities that help AI agents work more effectively with context and error recovery.

### **[03 - Context & Dependency System](coordination/03-context-dependency-system.md)**
Intelligent context sharing between agents and automatic dependency resolution.

---

## 📊 **Project Management Systems**

### **[16 - Project Management](project-management/16-project-management.md)**
High-level project creation, tracking, and completion management.

### **[04 - Kanban Integration](project-management/04-kanban-integration.md)**
Multi-provider Kanban board integration. SQLite (default, zero-setup) plus Planka, GitHub Projects, and Linear.

### **[24 - Analysis Tools](project-management/24-analysis-tools.md)**
Project analytics, performance metrics, and insight generation.

### **[54 - Hierarchical Task Decomposition](project-management/54-hierarchical-task-decomposition.md)**
AI-powered task breakdown into manageable subtasks with clear interfaces and shared conventions.

---

## 🔧 **Development & Analysis Systems**

### **[42 - Code Analysis System](development/42-code-analysis-system.md)**
Repository analysis, language detection, complexity assessment, and security scanning for agent-generated code.

---

## 🔒 **Security & Compliance Systems**

### **[51 - Security Systems](security/51-security-systems.md)**
Comprehensive security framework including authentication, threat detection, and workspace isolation.

---

## 🗄️ **Data & Storage Systems**

### **[10 - Persistence Layer](infrastructure/10-persistence-layer.md)**
Data storage abstraction supporting multiple backends (SQLite, JSON, PostgreSQL).

### **[32 - Core Models](infrastructure/32-core-models.md)**
Data models for tasks, agents, projects, and system entities.

### **[13 - Cost Tracking](operations/13-cost-tracking.md)**
API usage monitoring and cost optimization across AI providers.

### **[19 - NLP System](infrastructure/19-nlp-system.md)**
Natural language processing for task analysis and context extraction.

---

## ✅ **Quality & Testing Systems**

### **[18 - Quality Assurance](quality/18-quality-assurance.md)**
Automated quality checks, code review, and deployment validation.

### **[30 - Testing Framework](quality/30-testing-framework.md)**
Comprehensive testing infrastructure for all Marcus components.

### **[11 - Monitoring Systems](quality/11-monitoring-systems.md)**
Real-time system monitoring, health checks, and alerting.

### **[29 - Detection Systems](quality/29-detection-systems.md)**
Anomaly detection, error pattern recognition, and preventive measures.

---

## 🏭 **Infrastructure Systems**

### **[08 - Error Framework](infrastructure/08-error-framework.md)**
Comprehensive error handling with automatic recovery and escalation.

### **[09 - Event-Driven Architecture](architecture/09-event-driven-architecture.md)**
Publish/subscribe system for loose coupling and scalability.

### **[06 - MCP Server](architecture/06-mcp-server.md)**
Model Context Protocol server implementation for AI agent integration.

### **[14 - Workspace Isolation](infrastructure/14-workspace-isolation.md)**
Secure isolation between agents and projects.

### **[15 - Service Registry](architecture/15-service-registry.md)**
Dynamic service discovery and health management.

---

## ⚙️ **Operations Systems**

### **[22 - Operational Modes](operations/22-operational-modes.md)**
Different operation modes (development, staging, production) with appropriate configurations.

### **[28 - Configuration Management](infrastructure/28-configuration-management.md)**
Dynamic configuration system with environment-based overrides.

### **[31 - Resilience](infrastructure/31-resilience.md)**
Fault tolerance, circuit breakers, and graceful degradation.

---

## 🎨 **Visualization Systems**

### **[05 - Visualization System](visualization/05-visualization-system.md)**
Real-time dashboards and project visualization tools. The active visualization product is [Cato](https://github.com/painted-porch/cato), which reads board state directly.

### **[02 - Logging System](visualization/02-logging-system.md)**
Comprehensive logging infrastructure with multiple output formats.

---

## 🔍 **How to Use This Documentation**

### **For New Developers**
1. Start with **Core Intelligence** systems to understand how Marcus thinks
2. Review **Agent Coordination** to understand agent workflows
3. Check **Infrastructure** systems for integration patterns

### **For System Integration**
1. Review **MCP Server** (06) for API integration
2. Check **Error Framework** (08) for proper error handling
3. Study **Configuration Management** (28) for setup

### **For Extension Development**
1. Understand **Event-Driven Architecture** (09) for loose coupling
2. Review **Service Registry** (15) for service discovery
3. Check **Testing Framework** (30) for validation

### **For Operations**
1. Study **Monitoring Systems** (11) for observability
2. Review **Resilience** (31) for fault tolerance
3. Check **Pipeline Systems** (20) for deployment

---

## 🔗 **System Interdependencies**

```mermaid
graph TD
    A[01-Memory] --> B[07-AI Engine]
    B --> C[17-Learning]
    C --> D[27-Recommendations]

    E[21-Agent Coord] --> F[26-Worker Support]
    F --> G[03-Context System]
    G --> H[12-Communication]

    I[16-Project Mgmt] --> J[04-Kanban]
    J --> K[24-Analysis]
    K --> L[25-Reports]

    M[08-Error Framework] --> N[09-Events]
    N --> O[06-MCP Server]
    O --> P[15-Service Registry]
```

---

## 📈 **System Metrics**

- **55 Total Systems** documented
- **6 Core Intelligence** systems for AI decision-making and classification
- **4 Agent Coordination** systems for workflow management
- **2 Development & Analysis** systems for code quality and API integration
- **1 Security & Compliance** system for comprehensive security
- **7 Project Management** systems including workflow orchestration and task decomposition
- **5 Infrastructure** systems for reliability and scalability
- **4 Quality & Testing** systems for reliability
- **Rest**: Data storage, operations, and visualization systems

---

## 🤝 **Contributing to System Documentation**

When modifying systems:

1. **Update the relevant system documentation**
2. **Update this README** if adding/removing systems
3. **Update interdependency diagrams** if changing relationships
4. **Add integration examples** for new APIs
5. **Document breaking changes** clearly

---

## 💡 **Implementation Notes**

- All systems follow **Event-Driven Architecture** principles
- **Error Framework** provides consistent error handling across systems
- **Configuration Management** allows per-environment customization
- **Testing Framework** ensures all systems maintain quality standards
- **MCP Server** exposes unified API regardless of internal complexity

---

*Each system is designed to be independently testable, scalable, and maintainable while contributing to Marcus's overall AI agent coordination capabilities.*
