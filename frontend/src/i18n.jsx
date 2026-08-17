import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'

const messages = {
  'zh-CN': {
    aiTeam: 'AI 产品团队', signInTitle: '欢迎回来', signInDesc: '登录后继续构建产品。', email: '邮箱', password: '密码', signIn: '登录', testAccounts: '测试账号',
    loginHero: '把想法变成\n真正可用的产品。', loginCopy: '与专业 AI Agent 团队一起完成调研、规划、开发和迭代。', teamReady: 'AI 团队已就绪',
    newProject: '新建项目', allProjects: '全部项目', team: '团队', discover: '发现', settings: '设置', yourProjects: '你的项目', signOut: '退出登录',
    buildNext: '构建你的下一个想法', buildNextDesc: '从一段需求开始，让 AI 产品团队规划、编码并运行完整网站。', createProject: '创建项目',
    buildQuestion: '我们要构建什么？', buildDesc: '描述产品目标，团队会把它实现为带真实前后端的可用网站。', projectName: '项目名称', productBrief: '产品需求', teamMode: '团队模式', teamModeDesc: '规划、审批，然后构建', engineerMode: '工程师模式', engineerModeDesc: 'Alex 立即开始构建', startBuilding: '开始构建',
    workspace: '工作区', allProjectsDesc: 'AI 团队已规划、构建并可持续迭代的产品。', projects: '项目', livePreviews: '在线预览', specialists: '专业 Agent', recentProjects: '最近项目', total: '个项目',
    collaboration: '协作', yourTeam: '你的团队', yourTeamDesc: '专业 AI Agent 与工作区成员协同交付。', aiProductTeam: 'AI 产品团队', online: '在线', members: '工作区成员', accounts: '个账号', active: '活跃',
    starterLibrary: '模板库', discoverDesc: '从成熟产品形态开始，再让团队按你的需求定制。', all: '全部', useTemplate: '使用模板',
    preferences: '偏好设置', settingsDesc: '管理个人资料、界面和 Agent 默认配置。', profile: '个人资料', profileDesc: '你在工作区中的身份。', displayName: '显示名称', agentDefaults: 'Agent 默认值', agentDefaultsDesc: '创建新项目时自动应用。', defaultMode: '默认构建模式', compactActivity: '精简活动', compactDesc: '使用更短的 Agent 事件摘要。', appearance: '界面与语言', appearanceDesc: '选择语言和显示主题。', language: '语言', theme: '主题', light: '浅色', dark: '深色', runtime: '运行环境', runtimeDesc: '只读部署配置。', model: '模型', apiGateway: 'API 网关', projectDatabase: '项目数据库', saveChanges: '保存设置', settingsSaved: '设置已保存',
    cloud: 'Cloud', cloudTitle: '容器云', cloudDesc: '发布项目，并管理由 Atoms Cloud 隔离运行的应用与 PostgreSQL 容器。', deployments: '发布实例', containers: '容器', running: '运行中', dockerRuntime: 'Docker 运行时', isolatedDatabase: '项目专属 PostgreSQL', openApp: '打开应用', removeDeployment: '删除实例', removeDeploymentConfirm: '确定删除此应用实例吗？项目数据库容器和持久数据会保留，供后续发布继续使用。', noDeployments: '还没有发布实例', noDeploymentsDesc: '从项目工作区点击发布，Cloud 会构建镜像并启动完整运行栈。', containerManagement: '容器管理', managedOnly: '仅显示 Atoms Cloud 托管容器', container: '容器', role: '角色', image: '镜像', state: '状态', operations: '操作', logs: '日志与终端', stop: '停止', start: '启动', restart: '重启', noLogs: '暂无日志', execCommand: '容器内执行（JSON 参数数组）', run: '执行', commandArray: '命令必须是 JSON 字符串数组', publishing: '发布中',
    personalWorkspace: '个人工作区', export: '导出', publish: '发布', preview: '预览', code: '代码', activity: '活动', desktop: '桌面', mobile: '手机', refresh: '刷新',
    teamIntro: 'Mike 负责统筹，专业 Agent 按阶段交付并在关键决策处等待你的确认。', productPlan: '产品与架构计划', preparedBy: 'Mike 汇总 · Emma / Bob 交付', features: '项功能', pages: '页面', coreFeatures: '核心功能', architecture: '技术架构', decisions: '待确认决策', requestChanges: '要求修改', changePlaceholder: '说明计划需要怎样调整…', approveBuild: '批准并构建',
    waiting: '等待当前任务完成…', askTeam: '告诉团队需要修改或增加什么…', working: '正在工程文件中工作', planning: '正在组织产品与架构计划',
    projectFiles: '项目文件', filterFiles: '筛选文件', noFile: '未选择文件', format: '格式化', save: '保存', saved: '已保存', unsaved: '未保存', checksPassed: '检查通过', checksFailed: '检查未通过', changedLive: 'Agent 刚刚修改', lines: '行',
    agentActivity: 'Agent 工作流', toolCalls: '次工具调用', workflow: '工作流程', done: '已完成', inProgress: '进行中', pending: '等待中',
    previewOnline: '预览在线', localWorkspace: '本地工作区', unversioned: '未生成版本', approvePreview: '批准计划后开始构建', previewSoon: '实时预览将在这里出现', previewDesc: 'Alex 正在构建真实 FastAPI 后端和响应式前端。',
    status_ready: '就绪', status_created: '准备中', status_planning: '规划中', status_awaiting_approval: '等待批准', status_building: '构建中', status_testing: '验证中', status_preview_ready: '预览就绪', status_failed: '失败',
    saveError: '保存失败', exportError: '导出失败', formatError: '格式化失败',
  },
  'en-US': {
    aiTeam: 'AI product team', signInTitle: 'Welcome back', signInDesc: 'Sign in to continue building.', email: 'Email', password: 'Password', signIn: 'Sign in', testAccounts: 'Test accounts',
    loginHero: 'Turn ideas into\nworking products.', loginCopy: 'Research, plan, build and iterate with a specialized AI agent team.', teamReady: 'Your AI team is ready',
    newProject: 'New project', allProjects: 'All projects', team: 'Team', discover: 'Discover', settings: 'Settings', yourProjects: 'Your projects', signOut: 'Sign out',
    buildNext: 'Build your next idea', buildNextDesc: 'Start with a brief and let your AI product team plan, code and run a complete website.', createProject: 'Create project',
    buildQuestion: 'What will we build?', buildDesc: 'Describe the product. The team will turn it into a usable website with a real frontend and backend.', projectName: 'Project name', productBrief: 'Product brief', teamMode: 'Team mode', teamModeDesc: 'Plan, approve, then build', engineerMode: 'Engineer mode', engineerModeDesc: 'Alex builds immediately', startBuilding: 'Start building',
    workspace: 'Workspace', allProjectsDesc: 'Products your AI team has planned, built and kept ready to iterate.', projects: 'Projects', livePreviews: 'Live previews', specialists: 'Specialists', recentProjects: 'Recent projects', total: 'total',
    collaboration: 'Collaboration', yourTeam: 'Your team', yourTeamDesc: 'AI specialists and workspace members delivering together.', aiProductTeam: 'AI product team', online: 'online', members: 'Workspace members', accounts: 'accounts', active: 'Active',
    starterLibrary: 'Starter library', discoverDesc: 'Start from a proven product shape, then let the team make it yours.', all: 'All', useTemplate: 'Use template',
    preferences: 'Preferences', settingsDesc: 'Manage your profile, interface and agent defaults.', profile: 'Profile', profileDesc: 'Your identity inside this workspace.', displayName: 'Display name', agentDefaults: 'Agent defaults', agentDefaultsDesc: 'Applied when a new project starts.', defaultMode: 'Default build mode', compactActivity: 'Compact activity', compactDesc: 'Use shorter Agent event summaries.', appearance: 'Appearance & language', appearanceDesc: 'Choose your language and display theme.', language: 'Language', theme: 'Theme', light: 'Light', dark: 'Dark', runtime: 'Runtime', runtimeDesc: 'Read-only deployment configuration.', model: 'Model', apiGateway: 'API gateway', projectDatabase: 'Project database', saveChanges: 'Save changes', settingsSaved: 'Settings saved',
    cloud: 'Cloud', cloudTitle: 'Container cloud', cloudDesc: 'Publish projects and manage isolated application and PostgreSQL containers operated by Atoms Cloud.', deployments: 'Deployments', containers: 'Containers', running: 'Running', dockerRuntime: 'Docker runtime', isolatedDatabase: 'Project PostgreSQL', openApp: 'Open app', removeDeployment: 'Delete deployment', removeDeploymentConfirm: 'Delete this application deployment? The project database container and persistent data are retained for future deployments.', noDeployments: 'No deployments yet', noDeploymentsDesc: 'Publish from a project workspace. Cloud will build an image and start the complete runtime stack.', containerManagement: 'Container management', managedOnly: 'Atoms Cloud managed containers only', container: 'Container', role: 'Role', image: 'Image', state: 'State', operations: 'Operations', logs: 'Logs & terminal', stop: 'Stop', start: 'Start', restart: 'Restart', noLogs: 'No logs', execCommand: 'Execute in container (JSON argv)', run: 'Run', commandArray: 'Command must be a JSON string array', publishing: 'Publishing',
    personalWorkspace: 'Personal workspace', export: 'Export', publish: 'Publish', preview: 'Preview', code: 'Code', activity: 'Activity', desktop: 'Desktop', mobile: 'Mobile', refresh: 'Refresh',
    teamIntro: 'Mike coordinates the work, specialists deliver each stage, and meaningful decisions pause for your approval.', productPlan: 'Product & architecture plan', preparedBy: 'Compiled by Mike · Emma / Bob', features: 'features', pages: 'Pages', coreFeatures: 'Core features', architecture: 'Architecture', decisions: 'Decisions', requestChanges: 'Request changes', changePlaceholder: 'Describe how the plan should change…', approveBuild: 'Approve & build',
    waiting: 'Wait for the current run to finish…', askTeam: 'Ask the team to change or add anything…', working: 'Working in the project workspace', planning: 'Coordinating product and architecture',
    projectFiles: 'Project files', filterFiles: 'Filter files', noFile: 'No file selected', format: 'Format', save: 'Save', saved: 'Saved', unsaved: 'Unsaved', checksPassed: 'Checks passed', checksFailed: 'Checks failed', changedLive: 'Just changed by Agent', lines: 'lines',
    agentActivity: 'Agent workflow', toolCalls: 'tool calls', workflow: 'Workflow', done: 'Done', inProgress: 'In progress', pending: 'Pending',
    previewOnline: 'Preview online', localWorkspace: 'Local workspace', unversioned: 'Unversioned', approvePreview: 'Approve the plan to begin', previewSoon: 'Your live preview will appear here', previewDesc: 'Alex is building a real FastAPI backend and responsive frontend.',
    status_ready: 'Ready', status_created: 'Starting', status_planning: 'Planning', status_awaiting_approval: 'Needs approval', status_building: 'Building', status_testing: 'Testing', status_preview_ready: 'Preview ready', status_failed: 'Failed',
    saveError: 'Save failed', exportError: 'Export failed', formatError: 'Format failed',
  },
}

const UiContext = createContext(null)

export function UiProvider({ children }) {
  const [locale, setLocaleState] = useState(() => localStorage.getItem('atoms_locale') || 'zh-CN')
  const [theme, setThemeState] = useState(() => localStorage.getItem('atoms_theme') || 'light')
  const setLocale = value => { localStorage.setItem('atoms_locale', value); setLocaleState(value) }
  const setTheme = value => { localStorage.setItem('atoms_theme', value); setThemeState(value) }
  useEffect(() => { document.documentElement.lang = locale; document.documentElement.dataset.theme = theme; document.documentElement.style.colorScheme = theme }, [locale, theme])
  const value = useMemo(() => ({ locale, theme, setLocale, setTheme, t: key => messages[locale]?.[key] ?? messages['en-US'][key] ?? key }), [locale, theme])
  return <UiContext.Provider value={value}>{children}</UiContext.Provider>
}

export function useUi() { return useContext(UiContext) }
